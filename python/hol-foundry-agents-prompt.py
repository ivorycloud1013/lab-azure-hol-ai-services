#!/usr/bin/env python3

import argparse
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FileSearchTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError

import identity

INSTRUCTIONS = (
    "You answer questions about the documents attached to you. "
    "Search them before answering and cite the file you relied on. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

DEFAULT_AGENT_NAME = "hol-md-rag"
FILE_PURPOSE = "assistants"
MAX_SEARCH_RESULTS = 10


def parse_args():
    parser = argparse.ArgumentParser(
        description="Answer questions over a markdown file uploaded to a Foundry prompt agent.",
        epilog="The agent is reused when --agent-name already exists, so --file is only read the "
               "first time. Anything this run creates is rolled back if the run fails. "
               "--endpoint is the project endpoint, "
               "https://<resource>.ai.azure.com/api/projects/<project>. "
               "The vector store needs an embedding deployment on the account.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--file", metavar="MD",
                        help="markdown file to upload, required only when the agent does not exist yet")
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME,
                        help=f"agent to reuse or create (default {DEFAULT_AGENT_NAME})")
    parser.add_argument("--question", action="append", default=[], required=True, metavar="TEXT",
                        help="what to ask, repeat to follow up in the same conversation")
    parser.add_argument("--delete", action="store_true",
                        help="delete the agent, its vector store and its files when done")

    args = parser.parse_args()
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the projects SDK, use another method")
    if args.file and not os.path.isfile(args.file):
        parser.error(f"{args.file} not found")
    return args


def get_latest_version(project, agent_name):
    """The agent's latest version, or None when the agent does not exist yet.

    agents.get returns the agent, whose latest version carries the definition,
    while create_version returns that version straight away. Reducing to the
    version here leaves the rest of the code one shape to deal with.
    """
    try:
        return project.agents.get(agent_name).versions.latest
    except ResourceNotFoundError:
        return None


def build_agent(project, client, args, ledger):
    """Upload the document, index it, and declare the agent over it.

    Every step is appended to the ledger as it succeeds, so a failure halfway
    through leaves behind a list of exactly what needs undoing.
    """
    with open(args.file, "rb") as document:
        file_id = client.files.create(file=document, purpose=FILE_PURPOSE).id
    ledger.append((lambda: client.files.delete(file_id), f"file {file_id}"))
    print(f"uploaded {args.file} as {file_id}")

    vector_store_id = client.vector_stores.create(name=f"{args.agent_name}-corpus").id
    ledger.append((lambda: client.vector_stores.delete(vector_store_id), f"vector store {vector_store_id}"))

    batch = client.vector_stores.file_batches.create_and_poll(vector_store_id, file_ids=[file_id])
    if batch.status != "completed":
        raise SystemExit(f"indexing ended as {batch.status}: {batch.file_counts}")
    print(f"vector store {vector_store_id} indexed the document")

    # A prompt agent is declarative — the model, instructions and tools live in
    # the service, and file search runs there too. Nothing runs in this process.
    agent = project.agents.create_version(
        agent_name=args.agent_name,
        definition=PromptAgentDefinition(
            model=args.deployment,
            instructions=INSTRUCTIONS,
            tools=[FileSearchTool(vector_store_ids=[vector_store_id], max_num_results=MAX_SEARCH_RESULTS)],
        ),
    )
    ledger.append((lambda: project.agents.delete(args.agent_name), f"agent {args.agent_name}"))
    print(f"agent {agent.name} version {agent.version} created")
    return agent


def get_citations(response):
    """Collect the file names file search grounded the answer on, in first-seen order."""
    citations = []
    for item in response.output:
        for content in getattr(item, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                name = getattr(annotation, "filename", None)
                if name and name not in citations:
                    citations.append(name)
    return citations


def ask(client, conversation_id, question):
    print(f"\n> {question}")
    response = client.responses.create(conversation=conversation_id, input=question)
    print(response.output_text)
    citations = get_citations(response)
    if citations:
        print(f"  sources: {', '.join(citations)}")


def run_deletes(deletes):
    """Best effort — one failed delete must not hide the others."""
    for delete, target in deletes:
        try:
            delete()
            print(f"deleted {target}")
        except Exception as error:  # noqa: BLE001 — report and keep deleting
            print(f"could not delete {target}: {error}")


def plan_full_delete(project, client, args, agent):
    """Deletes for the whole agent, including one this run only reused.

    Agent first, then its vector stores, then the files — a vector store still
    holding a file refuses to let it go.
    """
    vector_store_ids = get_vector_store_ids(agent)
    file_ids = []
    for vector_store_id in vector_store_ids:
        try:
            file_ids += [entry.id for entry in client.vector_stores.files.list(vector_store_id)]
        except Exception as error:  # noqa: BLE001 — report and keep going
            print(f"could not list files of {vector_store_id}: {error}")

    return (
        [(lambda: project.agents.delete(args.agent_name), f"agent {args.agent_name}")]
        + [(lambda i=i: client.vector_stores.delete(i), f"vector store {i}") for i in vector_store_ids]
        + [(lambda i=i: client.files.delete(i), f"file {i}") for i in file_ids]
    )


def get_vector_store_ids(agent):
    """Read back what the agent searches, so a reused agent can still be cleaned up."""
    ids = []
    for tool in getattr(agent.definition, "tools", None) or []:
        ids.extend(getattr(tool, "vector_store_ids", None) or [])
    return ids


def main():
    args = parse_args()
    project = AIProjectClient(endpoint=args.endpoint, credential=identity.get_credential(args))
    client = project.get_openai_client()

    # What this run created, oldest first. Undoing it in reverse also happens to
    # be the order the service requires: agent, then vector store, then file.
    ledger = []

    try:
        agent = get_latest_version(project, args.agent_name)
        if agent is None:
            if not args.file:
                raise SystemExit(f"agent {args.agent_name} does not exist yet, pass --file to create it")
            agent = build_agent(project, client, args, ledger)
        else:
            print(f"reusing agent {agent.name} version {agent.version}")

        # Same client as above but bound to the agent, so responses run through it.
        agent_client = project.get_openai_client(agent_name=args.agent_name)
        conversation = agent_client.conversations.create()
        for question in args.question:
            ask(agent_client, conversation.id, question)
    except BaseException as error:
        # Roll back only what this run created — never an agent it merely reused.
        if ledger:
            print(f"\n{type(error).__name__}: {error}")
            print("rolling back what this run created")
            run_deletes(list(reversed(ledger)))
        project.close()
        raise

    if args.delete:
        run_deletes(plan_full_delete(project, client, args, agent))
    project.close()


if __name__ == "__main__":
    main()
