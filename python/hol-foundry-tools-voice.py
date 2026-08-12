#!/usr/bin/env python3

import argparse
import asyncio
import base64
import threading
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
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
from azure.core.exceptions import ResourceNotFoundError

import identity

INSTRUCTIONS = (
    "You are a spoken assistant in a hands-on lab. Keep answers to a few sentences, because "
    "they are read out loud. Say you do not know rather than guessing. "
    "Answer in the language you were spoken to."
)

DEFAULT_MODEL = "gpt-realtime"

# What --project-endpoint creates. The agent holds the instructions, so the voice
# session sends none of its own and the same agent answers over any other channel.
DEFAULT_AGENT_NAME = "hol-voice-agent"
DEFAULT_AGENT_DEPLOYMENT = "gpt-5.6-terra"

# Project endpoints end in /api/projects/<name>, and the voice session wants that
# trailing name rather than the URL.
PROJECT_PATH_MARKER = "/api/projects/"

DEFAULT_VOICE = "en-US-AvaMultilingualNeural"

# What pcm16 output comes back as. The other output formats exist, but this script
# only asks for the default one.
OUTPUT_SAMPLE_RATE = 24000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# The microphone runs at the same rate the answer comes back at, so one constant
# describes both ends of the conversation.
MIC_SAMPLE_RATE = 24000

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
               "one, whichever answers. --project-endpoint creates the agent here the way "
               "hol-foundry-tools-knowledge.py does, --agent-name talks to one that already "
               "exists, and neither leaves the bare model answering. You speak into the "
               "microphone and the answer is played back, so this needs the sounddevice package "
               "and a sound card.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry account endpoint")

    identity.add_auth_arguments(parser)

    target = parser.add_argument_group("who answers")
    target.add_argument("--model", help=f"realtime model to talk to (default {DEFAULT_MODEL})")
    target.add_argument("--project-endpoint", metavar="URL",
                        help="create a prompt agent in this project and talk to it, "
                             "https://<resource>.services.ai.azure.com/api/projects/<project>")
    target.add_argument("--deployment", metavar="NAME",
                        help=f"model the created agent runs on (default {DEFAULT_AGENT_DEPLOYMENT})")
    target.add_argument("--agent-name",
                        help="talk to this Foundry agent instead of a bare model, which brings "
                             f"its own instructions and tools — with --project-endpoint this is "
                             f"the agent to create instead (default {DEFAULT_AGENT_NAME})")
    target.add_argument("--project-name", help="project holding --agent-name")
    target.add_argument("--delete", action="store_true",
                        help="delete the agent --project-endpoint created when done")

    parser.add_argument("--seconds", type=float, metavar="N",
                        help="stop after N seconds instead of waiting for Ctrl+C")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Azure voice (default {DEFAULT_VOICE})")
    parser.add_argument("--language", help="language of the speech, e.g. ko-KR (default: detected)")
    parser.add_argument("--instructions", help="override what the assistant is told to be")

    args = parser.parse_args()
    if args.auth == "access-token":
        parser.error("--auth access-token is not supported by the Voice Live SDK, use another method")
    if args.auth == "api-key" and not args.api_key:
        parser.error("--auth api-key needs --api-key or AZURE_OPENAI_API_KEY")
    return validate_target(parser, args)


def validate_target(parser, args):
    """Settle who answers, so the rest of the run has one agent name and one project."""
    if args.project_endpoint:
        if args.project_name:
            parser.error("--project-endpoint already names the project, drop --project-name")
        # Filled in here so no later step has to ask which mode this run is in.
        args.agent_name = args.agent_name or DEFAULT_AGENT_NAME
        args.project_name = get_project_name(parser, args.project_endpoint)
    else:
        if args.deployment:
            parser.error("--deployment is the model of the agent --project-endpoint creates")
        if args.delete:
            parser.error("--delete only applies to an agent --project-endpoint created")
        if bool(args.agent_name) != bool(args.project_name):
            parser.error("--agent-name and --project-name go together")

    if args.agent_name and args.model:
        parser.error("the agent already carries a model, so --model does not apply")
    if args.agent_name and args.auth == "api-key":
        # Agent invocation is Entra only. Without this the run dials out and fails
        # on the websocket handshake instead of here.
        parser.error("agent mode does not support key authentication, use an Entra method")
    return args


def get_project_name(parser, endpoint):
    """The trailing segment of a project endpoint, which is what the session wants."""
    path = urlsplit(endpoint).path.rstrip("/")
    _, marker, name = path.rpartition(PROJECT_PATH_MARKER)
    if not marker or not name or "/" in name:
        parser.error(f"--project-endpoint {endpoint} must end in {PROJECT_PATH_MARKER}<project>")
    return name


def get_model(args):
    """The model to talk to, or None when an agent brings its own."""
    return None if args.agent_name else (args.model or DEFAULT_MODEL)


def agent_exists(project, agent_name):
    """Whether the agent predates this run, which decides if a failure may delete it."""
    try:
        project.agents.get(agent_name)
        return True
    except ResourceNotFoundError:
        return False


def create_agent(project, args):
    """A prompt agent is declarative — model and instructions live in the service.

    A new version every run, because the instructions are part of the definition
    and this script exists to let you change them and hear the difference. The
    version is returned so the session talks to exactly what was just created,
    rather than to whatever is latest by the time the websocket opens.
    """
    agent = project.agents.create_version(
        agent_name=args.agent_name,
        definition=PromptAgentDefinition(
            model=args.deployment or DEFAULT_AGENT_DEPLOYMENT,
            instructions=args.instructions or INSTRUCTIONS,
        ),
    )
    print(f"agent {agent.name} version {agent.version} created")
    return agent.version


def build_session(args):
    """What the session should be, in one object the service applies at once.

    Fields are only set once they have a value, because a field left out keeps the
    service's own default. The model is left out entirely — connect() already chose
    it, from --model or from the agent.
    """
    transcription = AudioInputTranscriptionOptions(model=TRANSCRIPTION_MODEL)
    if args.language:
        transcription.language = args.language

    session = RequestSession(
        modalities=[Modality.TEXT, Modality.AUDIO],
        voice=AzureStandardVoice(name=args.voice),
        input_audio_format=InputAudioFormat.PCM16,
        input_audio_sampling_rate=MIC_SAMPLE_RATE,
        input_audio_transcription=transcription,
        # The service decides where a turn ended, and may be interrupted mid-answer.
        turn_detection=AzureSemanticVad(interrupt_response=True),
    )
    # An agent brings its own instructions, and overwriting them with the default
    # of this script would throw away the reason for pointing at an agent at all.
    # An agent this run created already carries --instructions in its definition,
    # so repeating them here would only be a second copy of the same sentence.
    if not args.agent_name:
        session.instructions = args.instructions or INSTRUCTIONS
    elif args.instructions and not args.project_endpoint:
        session.instructions = args.instructions
    return session


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
        raise SystemExit("this needs the sounddevice package: pip install sounddevice") from None
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


async def receive(connection, speaker):
    """Read server events until the conversation is cut short, playing what arrives."""
    async for event in connection:
        if event.type == ServerEventType.ERROR:
            raise SystemExit(f"{event.error.type}: {event.error.message}")

        if event.type == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            print(f"\n> {event.transcript}")
        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            speaker.play(event.delta)
        elif event.type == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            print(event.transcript)
        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            # Barge-in: the assistant is talking and the user started anyway.
            speaker.stop()


async def run_microphone(connection, args, sounddevice):
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
                receive(connection, speaker),
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


def build_connect_arguments(args, credential, agent_version):
    """Which of the two ways to reach the service this run is using."""
    common = {"endpoint": args.endpoint, "credential": credential}
    if args.agent_name:
        return {**common, "agent_name": args.agent_name, "project_name": args.project_name,
                "agent_version": agent_version}
    return {**common, "model": get_model(args)}


async def converse(args, credential, agent_version, sounddevice):
    async with connect(**build_connect_arguments(args, credential, agent_version)) as connection:
        await connection.session.update(session=build_session(args))
        print(f"connected to {args.agent_name or get_model(args)}")
        await run_microphone(connection, args, sounddevice)


def talk(args, credential, agent_version, sounddevice):
    """The conversation itself, with Ctrl+C treated as a way to end it rather than
    as a failure."""
    try:
        asyncio.run(converse(args, credential, agent_version, sounddevice))
    except KeyboardInterrupt:
        print()


def delete_agent(project, agent_name):
    try:
        project.agents.delete(agent_name)
        print(f"deleted agent {agent_name}")
    except Exception as error:  # noqa: BLE001 — whatever brought us here matters more
        print(f"could not delete agent {agent_name}: {error}")


def talk_to_new_agent(args, credential, sounddevice):
    """Create the agent, talk to it, and leave it behind unless asked otherwise."""
    project = AIProjectClient(endpoint=args.project_endpoint, credential=credential)
    existed = agent_exists(project, args.agent_name)
    delete_when_done = args.delete
    try:
        talk(args, credential, create_agent(project, args), sounddevice)
    except BaseException:
        # Roll back only an agent this run brought into existence, never one it
        # merely added a version to.
        delete_when_done = not existed
        raise
    finally:
        if delete_when_done:
            delete_agent(project, args.agent_name)
        project.close()


def main():
    args = parse_args()

    # Settled before anything reaches Azure. This used to happen inside converse(),
    # which is after the agent has already been created, so a machine without
    # PortAudio paid for an agent create and delete round trip before being told it
    # could not record anything.
    sounddevice = import_sounddevice()

    credential = create_credential(args)

    if args.project_endpoint:
        talk_to_new_agent(args, credential, sounddevice)
    else:
        talk(args, credential, None, sounddevice)


if __name__ == "__main__":
    main()
