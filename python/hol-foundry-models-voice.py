#!/usr/bin/env python3

import argparse

import azure.cognitiveservices.speech as speechsdk

import identity


def parse_args():
    parser = argparse.ArgumentParser(description="Synthesize speech (TTS) or transcribe audio (STT) with Azure Speech.")
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint (custom domain)")

    identity.add_auth_arguments(parser)

    parser.add_argument("--text", help="TTS: synthesize this text into --out")
    parser.add_argument("--transcribe", metavar="WAV", help="STT: transcribe this audio file")
    parser.add_argument("--out", default="speech.wav", help="TTS output file")
    parser.add_argument("--voice", default="en-US-AvaMultilingualNeural", help="TTS voice, e.g. ko-KR-SunHiNeural")
    parser.add_argument("--language", default="ko-KR", help="STT recognition language, e.g. ko-KR")

    args = parser.parse_args()
    if bool(args.text) == bool(args.transcribe):
        parser.error("pass exactly one of --text or --transcribe")
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
    config.speech_synthesis_voice_name = args.voice
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=config,
        audio_config=speechsdk.audio.AudioOutputConfig(filename=args.out),
    )
    result = synthesizer.speak_text_async(args.text).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise SystemExit(result.cancellation_details)
    print(args.out)


def transcribe(config, args):
    config.speech_recognition_language = args.language
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=config,
        audio_config=speechsdk.audio.AudioConfig(filename=args.transcribe),
    )
    # One utterance only. Longer audio needs start_continuous_recognition_async.
    result = recognizer.recognize_once_async().get()
    if result.reason != speechsdk.ResultReason.RecognizedSpeech:
        raise SystemExit(result.reason)
    print(result.text)


def main():
    args = parse_args()
    config = create_config(args)
    if args.text:
        synthesize(config, args)
    else:
        transcribe(config, args)


if __name__ == "__main__":
    main()
