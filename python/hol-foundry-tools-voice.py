#!/usr/bin/env python3

import argparse
import asyncio
import base64
import os
import threading
import wave

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioInputTranscriptionOptions,
    AzureSemanticVad,
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    RequestSession,
    ServerEventType,
)
from azure.core.credentials import AzureKeyCredential

import identity

INSTRUCTIONS = (
    "You are a spoken assistant in a hands-on lab. Keep answers to a few sentences, because "
    "they are read out loud. Say you do not know rather than guessing. "
    "Answer in the language you were spoken to."
)

DEFAULT_MODEL = "gpt-realtime"
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"
DEFAULT_AUDIO_OUT = "voice-out.wav"

# Sampling rates the service accepts for pcm16. Anything else has to be resampled
# before it gets here.
INPUT_SAMPLE_RATES = (8000, 16000, 24000)

# What pcm16 output comes back as. The other output formats exist, but this script
# only asks for the default one.
OUTPUT_SAMPLE_RATE = 24000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# The microphone runs at the same rate the answer comes back at, so one constant
# describes both ends of the conversation.
MIC_SAMPLE_RATE = 24000

# 200 ms of 24 kHz pcm16. Small enough that the service hears speech start promptly,
# large enough that a long file does not turn into thousands of websocket frames.
CHUNK_BYTES = MIC_SAMPLE_RATE * SAMPLE_WIDTH_BYTES // 5

# The sound card asks for this many frames at a time. Shorter blocks mean lower
# latency and more callbacks.
MIC_BLOCK_FRAMES = 2400

TRANSCRIPTION_MODEL = "azure-speech"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Hold a spoken conversation with a Foundry model or agent over the Voice Live API.",
        epilog="One websocket carries speech in and speech out — no separate STT and TTS calls, "
               "which is what hol-foundry-models-stt_tts.py does instead. --endpoint is the "
               "account endpoint, https://<resource>.cognitiveservices.azure.com, not the project "
               "one. --mic needs the sounddevice package and a sound card, so a jumpbox reached "
               "over Bastion wants --audio-in instead.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")

    identity.add_auth_arguments(parser)

    target = parser.add_argument_group("who answers")
    target.add_argument("--model", help=f"realtime model to talk to (default {DEFAULT_MODEL})")
    target.add_argument("--agent-name", help="talk to this Foundry agent instead of a bare model, "
                                             "which brings its own instructions and tools")
    target.add_argument("--project-name", help="project holding --agent-name")
    target.add_argument("--agent-version", help="pin a version of --agent-name (default: the latest)")

    source = parser.add_argument_group("where the speech comes from")
    source.add_argument("--audio-in", metavar="WAV",
                        help="speak this file, 16-bit mono PCM at 8/16/24 kHz")
    source.add_argument("--mic", action="store_true", help="speak into the microphone until Ctrl+C")
    source.add_argument("--seconds", type=float, metavar="N",
                        help="stop --mic after N seconds instead of waiting for Ctrl+C")

    parser.add_argument("--audio-out", default=DEFAULT_AUDIO_OUT,
                        help=f"where the spoken answer is written (default {DEFAULT_AUDIO_OUT})")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Azure voice (default {DEFAULT_VOICE})")
    parser.add_argument("--language", help="language of the speech, e.g. ko-KR (default: detected)")
    parser.add_argument("--instructions", help="override what the assistant is told to be")

    args = parser.parse_args()
    if args.auth == "access-token":
        parser.error("--auth access-token is not supported by the Voice Live SDK, use another method")
    if args.auth == "api-key" and not args.api_key:
        parser.error("--auth api-key needs --api-key or AZURE_OPENAI_API_KEY")
    if bool(args.audio_in) == bool(args.mic):
        parser.error("pass exactly one of --audio-in or --mic")
    if args.audio_in and not os.path.isfile(args.audio_in):
        parser.error(f"{args.audio_in} not found")
    if args.seconds and not args.mic:
        parser.error("--seconds only applies to --mic")
    if bool(args.agent_name) != bool(args.project_name):
        parser.error("--agent-name and --project-name go together")
    if args.agent_name and args.model:
        parser.error("--agent-name already carries a model, so --model does not apply")
    if args.agent_name and args.auth == "api-key":
        # Agent invocation is Entra only. Without this the run dials out and fails
        # on the websocket handshake instead of here.
        parser.error("agent mode does not support key authentication, use an Entra method")
    return args


def read_wave(path):
    """Read a WAV file as raw pcm16, refusing anything the service cannot take.

    Failing here beats sending audio the service accepts and then hears as noise.
    """
    with wave.open(path, "rb") as audio:
        rate, channels, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        frames = audio.readframes(audio.getnframes())

    if channels != CHANNELS or width != SAMPLE_WIDTH_BYTES or rate not in INPUT_SAMPLE_RATES:
        raise SystemExit(
            f"{path} is {rate} Hz, {channels} channel(s), {width * 8}-bit — needs 16-bit mono at "
            f"{'/'.join(str(r) for r in INPUT_SAMPLE_RATES)} Hz. Convert it with: "
            f"ffmpeg -i {path} -ac 1 -ar 24000 -sample_fmt s16 converted.wav"
        )
    return frames, rate


def write_wave(path, audio):
    with wave.open(path, "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH_BYTES)
        out.setframerate(OUTPUT_SAMPLE_RATE)
        out.writeframes(audio)


def get_model(args):
    """The model to talk to, or None when an agent brings its own."""
    return None if args.agent_name else (args.model or DEFAULT_MODEL)


def build_session(args, sample_rate):
    """What the session should be, in one object the service applies at once.

    Fields are only set once they have a value: a field left out keeps the
    service's own default, while one set to None is sent as an explicit null and
    overwrites that default. The model is left out entirely — connect() already
    chose it, from --model or from the agent.

    --mic and --audio-in differ in one place: with a microphone the service decides
    when a turn ended, and with a file this script does, because the turn is the
    whole file.
    """
    transcription = AudioInputTranscriptionOptions(model=TRANSCRIPTION_MODEL)
    if args.language:
        transcription.language = args.language

    session = RequestSession(
        modalities=[Modality.TEXT, Modality.AUDIO],
        voice=AzureStandardVoice(name=args.voice),
        input_audio_format=InputAudioFormat.PCM16,
        input_audio_sampling_rate=sample_rate,
        input_audio_transcription=transcription,
    )
    # An agent brings its own instructions, and overwriting them with the default
    # of this script would throw away the reason for pointing at an agent at all.
    if args.instructions or not args.agent_name:
        session.instructions = args.instructions or INSTRUCTIONS

    if args.mic:
        session.turn_detection = AzureSemanticVad(interrupt_response=True)
    else:
        # Explicitly null, not merely absent: absent leaves the service listening
        # for a pause that a file streamed at full speed never contains.
        session["turn_detection"] = None
    return session


async def send_audio(connection, audio):
    """Push raw pcm16 into the input buffer in chunks."""
    for start in range(0, len(audio), CHUNK_BYTES):
        chunk = audio[start:start + CHUNK_BYTES]
        await connection.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())


class Speaker:
    """Plays what arrives while the rest of the answer is still arriving.

    The sound card pulls from a buffer on its own thread, so appending to it must
    never block — which is why this is a buffer and not a write() call.
    """

    def __init__(self, sounddevice):
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream = sounddevice.RawOutputStream(
            samplerate=OUTPUT_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=MIC_BLOCK_FRAMES, callback=self._fill,
        )

    def _fill(self, outdata, frames, time_info, status):
        wanted = len(outdata)
        with self._lock:
            chunk = bytes(self._buffer[:wanted])
            del self._buffer[:wanted]
        outdata[:len(chunk)] = chunk
        # Silence rather than whatever was in the buffer last time.
        outdata[len(chunk):] = b"\x00" * (wanted - len(chunk))

    def play(self, audio):
        with self._lock:
            self._buffer.extend(audio)

    def stop(self):
        """Drop what is queued, so an interrupted answer stops mid-sentence."""
        with self._lock:
            self._buffer.clear()

    def __enter__(self):
        self._stream.start()
        return self

    def __exit__(self, *_exception):
        self._stream.stop()
        self._stream.close()


def import_sounddevice():
    try:
        import sounddevice
    except OSError as error:  # PortAudio itself is missing
        raise SystemExit(f"sounddevice could not load PortAudio: {error}") from None
    except ImportError:
        raise SystemExit("--mic needs the sounddevice package: pip install sounddevice") from None
    return sounddevice


async def send_microphone(connection, queue):
    """Forward microphone blocks until the task is cancelled."""
    while True:
        chunk = await queue.get()
        await connection.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())


def open_microphone(sounddevice, loop, queue):
    def on_block(indata, frames, time_info, status):
        # Called on the sound card's thread, so hand the bytes over rather than
        # touching the event loop from here.
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    return sounddevice.RawInputStream(
        samplerate=MIC_SAMPLE_RATE, channels=CHANNELS, dtype="int16",
        blocksize=MIC_BLOCK_FRAMES, callback=on_block,
    )


async def receive(connection, spoken, speaker, stop_after_response):
    """Read server events until the answer is done, appending audio to `spoken`.

    The caller owns the buffer so that a conversation cut short by Ctrl+C still
    leaves behind everything that had been said by then.
    """
    async for event in connection:
        if event.type == ServerEventType.ERROR:
            raise SystemExit(f"{event.error.type}: {event.error.message}")

        if event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            print(f"\n> {event.transcript}")
        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            spoken.extend(event.delta)
            if speaker:
                speaker.play(event.delta)
        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            print(event.transcript)
        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED and speaker:
            # Barge-in: the assistant is talking and the user started anyway.
            speaker.stop()
        elif event.type == ServerEventType.RESPONSE_DONE and stop_after_response:
            return


async def run_file(connection, audio, spoken):
    """Speak the file, close the turn, and wait for the one answer it earns."""
    await send_audio(connection, audio)
    await connection.input_audio_buffer.commit()
    await connection.response.create()
    await receive(connection, spoken, speaker=None, stop_after_response=True)


async def run_microphone(connection, args, spoken, sounddevice):
    """Talk until Ctrl+C or --seconds, with the service deciding where turns end."""
    queue = asyncio.Queue()
    microphone = open_microphone(sounddevice, asyncio.get_running_loop(), queue)

    print("listening — Ctrl+C to stop")
    with Speaker(sounddevice) as speaker, microphone:
        # Sending and receiving have to run at once, or the assistant could not be
        # interrupted while it is still speaking.
        sender = asyncio.create_task(send_microphone(connection, queue))
        try:
            await asyncio.wait_for(
                receive(connection, spoken, speaker, stop_after_response=False),
                timeout=args.seconds,  # None waits for Ctrl+C instead
            )
        except asyncio.TimeoutError:
            pass
        finally:
            sender.cancel()
            # Let the cancellation land, so shutdown is quiet rather than a
            # warning about a task destroyed while still pending.
            await asyncio.gather(sender, return_exceptions=True)


def create_credential(args):
    """The Voice Live SDK asks the credential for a token itself, so the keyless
    path needs no bearer token provider."""
    if args.auth == "api-key":
        return AzureKeyCredential(args.api_key)
    return identity.get_credential(args)


def build_connect_arguments(args):
    """Which of the two ways to reach the service this run is using."""
    common = {"endpoint": args.endpoint, "credential": create_credential(args)}
    if args.agent_name:
        return {**common, "agent_name": args.agent_name, "project_name": args.project_name,
                "agent_version": args.agent_version}
    return {**common, "model": get_model(args)}


async def converse(args, spoken):
    # Both input paths are settled before dialing out, so an unusable file or a
    # machine without a sound card costs nothing.
    audio, sample_rate = read_wave(args.audio_in) if args.audio_in else (None, MIC_SAMPLE_RATE)
    sounddevice = import_sounddevice() if args.mic else None

    async with connect(**build_connect_arguments(args)) as connection:
        await connection.session.update(session=build_session(args, sample_rate))
        print(f"connected to {args.agent_name or get_model(args)}")

        if args.audio_in:
            await run_file(connection, audio, spoken)
        else:
            await run_microphone(connection, args, spoken, sounddevice)


def main():
    args = parse_args()
    spoken = bytearray()
    try:
        asyncio.run(converse(args, spoken))
    except KeyboardInterrupt:
        print()

    if not spoken:
        print("no audio came back")
        return
    write_wave(args.audio_out, bytes(spoken))
    print(f"\n{args.audio_out}")


if __name__ == "__main__":
    main()
