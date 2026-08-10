#!/usr/bin/env python3

import argparse
import json
import os
import threading
import wave

import azure.cognitiveservices.speech as speechsdk

import identity

# Speech 가 가장 잘 듣는 형식. 다른 형식도 받아 주지만 안에서 다운믹스·리샘플되고
# 그만큼 인식률이 깎인다.
STT_SAMPLE_RATE = 16000
STT_CHANNELS = 1
STT_SAMPLE_WIDTH_BYTES = 2

# TrueText 후처리. 문장 부호, 대소문자, 더듬거림 제거, 숫자 표기를 정리해 준다.
# 인식 자체가 아니라 표시용 텍스트를 다듬는 단계다.
POST_PROCESSING_TRUETEXT = "TrueText"

# 인식이 끝나기를 기다리는 한도. 세션이 끝났다는 신호를 못 받아도 영원히 매달리지 않는다.
RECOGNITION_TIMEOUT_SECONDS = 600


def parse_args():
    parser = argparse.ArgumentParser(
        description="Synthesize speech (TTS) or transcribe audio (STT) with Azure Speech.",
        epilog="STT reads the whole file, not just its first sentence. Recognition quality "
               "depends mostly on the input format and on --stt-phrase: 16 kHz mono PCM and a "
               "few domain terms are worth more than any other knob here.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint (custom domain)")

    identity.add_auth_arguments(parser)

    parser.add_argument("--tts-input", help="TTS: synthesize this text into --tts-output")
    parser.add_argument("--stt-input", metavar="WAV", help="STT: transcribe this audio file")
    parser.add_argument("--tts-output", default="speech.wav", help="TTS output file")
    parser.add_argument("--tts-voice", default="en-US-AvaMultilingualNeural", help="TTS voice, e.g. ko-KR-SunHiNeural")
    parser.add_argument("--stt-lang", default="ko-KR", help="STT recognition language, e.g. ko-KR")

    quality = parser.add_argument_group("STT 인식 품질")
    quality.add_argument("--stt-phrase", action="append", default=[], metavar="TEXT",
                         help="이 말이 나올 수 있다고 미리 알려 준다. 제품명·고유명사에 가장 효과가 크다. "
                              "반복 지정할 수 있다.")
    quality.add_argument("--stt-silence-ms", type=int, metavar="MS",
                         help="이만큼 조용하면 한 문장이 끝난 것으로 본다. 문장이 잘게 쪼개지면 늘린다.")
    quality.add_argument("--no-post-refine", action="store_true",
                         help=f"{POST_PROCESSING_TRUETEXT} 후처리를 끈다. 기본은 켜져 있다.")
    quality.add_argument("--stt-detailed", action="store_true",
                         help="후보와 신뢰도를 함께 보여 준다. 좋아졌는지 눈대중 말고 재려면 쓴다.")
    quality.add_argument("--stt-any-format", action="store_true",
                         help=f"{STT_SAMPLE_RATE//1000} kHz 모노가 아닌 파일도 그냥 보낸다")

    args = parser.parse_args()
    if bool(args.tts_input) == bool(args.stt_input):
        parser.error("pass exactly one of --tts-input or --stt-input")
    if args.stt_input and not os.path.isfile(args.stt_input):
        parser.error(f"{args.stt_input} not found")
    return args


def create_config(args):
    # The Speech SDK takes the credential object and asks it for a token itself,
    # so this path stays keyless without a bearer token provider.
    if args.auth == "api-key":
        return speechsdk.SpeechConfig(subscription=args.api_key, endpoint=args.endpoint)
    if args.auth == "access-token":
        raise SystemExit("--auth access-token is not supported by the Speech SDK, use another method")
    return speechsdk.SpeechConfig(token_credential=identity.get_credential(args), endpoint=args.endpoint)


def synthesize(config, args):
    config.speech_synthesis_voice_name = args.tts_voice
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=config,
        audio_config=speechsdk.audio.AudioOutputConfig(filename=args.tts_output),
    )
    result = synthesizer.speak_text_async(args.tts_input).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise SystemExit(result.cancellation_details)
    print(args.tts_output)


def check_audio_format(path):
    """맞지 않는 형식이면 왜 그런지와 고치는 명령을 알려 준다.

    조용히 변환하지 않는 이유는, 변환했다는 사실 자체가 인식 결과를 해석할 때
    알아야 하는 정보이기 때문이다.
    """
    try:
        with wave.open(path, "rb") as audio:
            rate, channels = audio.getframerate(), audio.getnchannels()
            width, frames = audio.getsampwidth(), audio.getnframes()
    except wave.Error as error:
        # WAV 가 아니어도 SDK 가 처리할 수 있는 형식이 있으므로 막지 않는다.
        print(f"  형식을 읽지 못했습니다({error}). 그대로 보냅니다.")
        return

    print(f"  입력: {rate} Hz, {channels}채널, {width * 8}bit, {frames / rate:.1f}초")
    if (rate, channels, width) == (STT_SAMPLE_RATE, STT_CHANNELS, STT_SAMPLE_WIDTH_BYTES):
        return

    raise SystemExit(
        f"{path} 는 {rate} Hz, {channels}채널입니다. Speech 는 "
        f"{STT_SAMPLE_RATE} Hz 모노 16bit 를 가장 잘 듣고, 그 밖의 형식은 안에서 "
        f"다운믹스·리샘플되면서 인식률이 떨어집니다. 이렇게 바꾸세요:\n"
        f"  ffmpeg -i {path!r} -ac {STT_CHANNELS} -ar {STT_SAMPLE_RATE} "
        f"-sample_fmt s16 converted.wav\n"
        f"지금 형식 그대로 보내려면 --stt-any-format 을 주세요."
    )


def apply_recognition_options(config, args):
    config.speech_recognition_language = args.stt_lang

    if not args.no_post_refine:
        # 인식 결과를 사람이 읽을 문장으로 다듬는다. 문장 부호와 숫자 표기가 이 단계에서 붙는다.
        config.set_property(
            speechsdk.PropertyId.SpeechServiceResponse_PostProcessingOption,
            POST_PROCESSING_TRUETEXT,
        )
    if args.stt_silence_ms:
        config.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(args.stt_silence_ms),
        )
    if args.stt_detailed:
        config.output_format = speechsdk.OutputFormat.Detailed


def add_phrases(recognizer, phrases):
    """나올 법한 말을 미리 알려 준다. 모델을 바꾸지 않고 얻는 가장 큰 이득이다."""
    if not phrases:
        return
    grammar = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
    for phrase in phrases:
        grammar.addPhrase(phrase)
    print(f"  구문 목록 {len(phrases)}개: {', '.join(phrases)}")


def print_alternatives(result):
    """상세 출력의 후보와 신뢰도. 어디서 흔들리는지 보려는 것이다."""
    raw = result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
    if not raw:
        return
    try:
        best = json.loads(raw).get("NBest") or []
    except json.JSONDecodeError:
        return
    for candidate in best[:3]:
        print(f"    신뢰도 {candidate.get('Confidence', 0):.3f} | {candidate.get('Display', '')}")


def transcribe(config, args):
    """파일 전체를 인식한다.

    recognize_once 는 첫 발화 하나만 듣고 끝나기 때문에, 몇 초를 넘는 파일은
    대부분이 조용히 버려진다. 그래서 연속 인식을 쓰고 조각을 모은다.
    """
    if not args.stt_any_format:
        check_audio_format(args.stt_input)
    apply_recognition_options(config, args)

    recognizer = speechsdk.SpeechRecognizer(
        speech_config=config,
        audio_config=speechsdk.audio.AudioConfig(filename=args.stt_input),
    )
    add_phrases(recognizer, args.stt_phrase)

    parts, failures, done = [], [], threading.Event()

    def on_recognized(event):
        if event.result.reason == speechsdk.ResultReason.RecognizedSpeech and event.result.text:
            parts.append(event.result.text)
            if args.stt_detailed:
                print(f"  [{len(parts)}] {event.result.text}")
                print_alternatives(event.result)

    def on_canceled(event):
        # 취소는 대개 인증이나 형식 문제다. 삼키면 빈 결과와 구별되지 않는다.
        details = event.result.cancellation_details
        if details and details.reason == speechsdk.CancellationReason.Error:
            failures.append(f"{details.reason}: {details.error_details}")
        done.set()

    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)
    recognizer.session_stopped.connect(lambda _event: done.set())

    recognizer.start_continuous_recognition_async().get()
    finished = done.wait(timeout=RECOGNITION_TIMEOUT_SECONDS)
    recognizer.stop_continuous_recognition_async().get()

    if failures:
        raise SystemExit("\n".join(failures))
    if not finished:
        print(f"  {RECOGNITION_TIMEOUT_SECONDS}초 안에 끝나지 않아 여기까지만 보여 줍니다.")
    if not parts:
        raise SystemExit("인식된 말이 없습니다. --stt-lang 과 입력 형식을 확인하세요.")

    print(" ".join(parts))


def main():
    args = parse_args()
    config = create_config(args)
    if args.tts_input:
        synthesize(config, args)
    else:
        transcribe(config, args)


if __name__ == "__main__":
    main()
