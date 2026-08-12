#!/usr/bin/env python3

import argparse

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchTool,
    AzureAISearchToolResource,
    BingGroundingSearchConfiguration,
    BingGroundingSearchToolParameters,
    BingGroundingTool,
    ConnectionType,
    PromptAgentDefinition,
)
from azure.core.exceptions import ResourceNotFoundError

import identity

INSTRUCTIONS = (
    "You answer questions from the knowledge sources attached to you and from nothing else. "
    "Search before answering, and name the document, merchant or article each fact came from. "
    "When the sources disagree, say so instead of picking one. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

DEFAULT_AGENT_NAME = "hol-knowledge-rag"

# The indexes aisrch-init-upload-documents.py builds. Passing one of these is the
# common case, but any index on the connected service works.
LAB_INDEXES = ("housing", "merchants", "news")

# Every query type but simple and semantic asks Search to embed the question, which
# only works when the index carries a vectorizer on its vector profile. The lab
# indexes embed at upload time and have none, so the vector types would only return
# an error here — the vectors are still searchable, just not from this tool. That
# leaves semantic as the best this lab can do, so it is fixed rather than offered.
QUERY_TYPE = "semantic"

# How many documents Search puts in front of the model.
TOP_K = 5

# Output items that are neither the answer nor the model thinking about it — one
# per search the agent ran.
QUIET_OUTPUT_TYPES = ("message", "reasoning")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ground a Foundry prompt agent on an Azure AI Search index and ask it questions.",
        epilog="Run aisrch-init-upload-documents.py first — it builds the "
               f"{', '.join(LAB_INDEXES)} indexes this reads. The agent searches as the project "
               "identity through a project connection, so the project needs Search Index Data "
               "Reader on the service, not you. --endpoint is the project endpoint, "
               "https://<resource>.ai.azure.com/api/projects/<project>. "
               "The agent outlives the run unless --delete, so hol-foundry-tools-voice.py "
               "--agent-name can then ask the same knowledge the same things out loud.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    knowledge = parser.add_argument_group("knowledge sources")
    knowledge.add_argument("--index", metavar="NAME",
                           help=f"Azure AI Search index to ground on, e.g. {' / '.join(LAB_INDEXES)}")
    knowledge.add_argument("--search-connection", metavar="NAME",
                           help="project connection to the Search service "
                                "(default: the project's default Azure AI Search connection)")
    knowledge.add_argument("--bing-connection", metavar="NAME",
                           help="project connection to Grounding with Bing Search, to answer from "
                                "the public web as well — no default, connections vary by project")

    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME,
                        help=f"agent to create a version of (default {DEFAULT_AGENT_NAME})")
    parser.add_argument("--question", action="append", default=[], required=True, metavar="TEXT",
                        help="what to ask, repeat to follow up in the same conversation")
    parser.add_argument("--show-sources", action="store_true",
                        help="print every search the agent ran and what it cited")
    parser.add_argument("--delete", action="store_true", help="delete the agent when done")

    args = parser.parse_args()
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the projects SDK, use another method")
    if not (args.index or args.bing_connection):
        parser.error("attach at least one source with --index or --bing-connection")
    if args.search_connection and not args.index:
        parser.error("--search-connection needs an --index to read")
    return args


def get_connection_id(project, name):
    """Resolve a connection by the name shown in the portal."""
    try:
        return project.connections.get(name).id
    except ResourceNotFoundError:
        raise SystemExit(f"the project has no connection named {name}") from None


def get_default_search_connection_id(project):
    try:
        return project.connections.get_default(ConnectionType.AZURE_AI_SEARCH).id
    except ValueError:
        raise SystemExit("the project has no default Azure AI Search connection, "
                         "pass --search-connection with its name") from None


def build_search_tool(project, args):
    """Declare the index as knowledge on the agent.

    The service queries Search itself over the connection, so nothing is retrieved
    in this process and the index never has to be reachable from here.
    """
    connection_id = (get_connection_id(project, args.search_connection) if args.search_connection
                     else get_default_search_connection_id(project))
    index = AISearchIndexResource(
        project_connection_id=connection_id,
        index_name=args.index,
        query_type=QUERY_TYPE,
        top_k=TOP_K,
    )
    print(f"grounding on index {args.index} ({QUERY_TYPE}, top {TOP_K})")
    # One index resource per agent is the service limit, so this list never grows.
    return AzureAISearchTool(azure_ai_search=AzureAISearchToolResource(indexes=[index]))


def build_bing_tool(project, args):
    connection_id = get_connection_id(project, args.bing_connection)
    print(f"grounding on the public web through {args.bing_connection}")
    return BingGroundingTool(
        bing_grounding=BingGroundingSearchToolParameters(
            search_configurations=[BingGroundingSearchConfiguration(project_connection_id=connection_id)],
        )
    )


def build_tools(project, args):
    tools = []
    if args.index:
        tools.append(build_search_tool(project, args))
    if args.bing_connection:
        tools.append(build_bing_tool(project, args))
    return tools


def create_version(project, args):
    """A prompt agent is declarative — model, instructions and knowledge live in
    the service, which retrieves and answers on its own.

    A new version every run, because the knowledge sources are part of the
    definition and this script exists to let you change them and compare.
    """
    agent = project.agents.create_version(
        agent_name=args.agent_name,
        definition=PromptAgentDefinition(
            model=args.deployment,
            instructions=INSTRUCTIONS,
            tools=build_tools(project, args),
        ),
    )
    print(f"agent {agent.name} version {agent.version} created")
    return agent


def agent_exists(project, agent_name):
    """Whether the agent predates this run, which decides if a failure may delete it."""
    try:
        project.agents.get(agent_name)
        return True
    except ResourceNotFoundError:
        return False


def describe_annotation(annotation):
    """One citation as a line of text.

    File search names a file, Search and Bing hand back a title and a URL, and
    which of those is present depends on the source — so take what is there.
    """
    title = (getattr(annotation, "filename", None) or getattr(annotation, "title", None)
             or getattr(annotation, "text", None))
    url = getattr(annotation, "url", None)
    if title and url and url not in title:
        return f"{title} — {url}"
    return title or url


def get_citations(response):
    """Collect what the answer was grounded on, in first-seen order."""
    citations = []
    for item in response.output:
        for content in getattr(item, "content", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                citation = describe_annotation(annotation)
                if citation and citation not in citations:
                    citations.append(citation)
    return citations


def print_search_activity(response):
    """Retrieval shows up in the output as its own items, next to the answer."""
    for item in response.output:
        if item.type in QUIET_OUTPUT_TYPES:
            continue
        queries = getattr(item, "queries", None) or []
        status = getattr(item, "status", None)
        detail = ", ".join(str(query) for query in queries) or status or ""
        print(f"  [{item.type}{': ' + detail if detail else ''}]")


def ask(client, conversation_id, question, show_sources):
    print(f"\n> {question}")
    response = client.responses.create(conversation=conversation_id, input=question)
    if show_sources:
        print_search_activity(response)
    print(response.output_text)
    citations = get_citations(response)
    if citations:
        print("  sources: " + "; ".join(citations))


def main():
    args = parse_args()
    project = AIProjectClient(endpoint=args.endpoint, credential=identity.get_credential(args))

    existed = agent_exists(project, args.agent_name)
    try:
        agent = create_version(project, args)
        # Same client bound to the agent, so responses run through its knowledge.
        agent_client = project.get_openai_client(agent_name=agent.name)
        conversation = agent_client.conversations.create()
        for question in args.question:
            ask(agent_client, conversation.id, question, args.show_sources)
    except BaseException as error:
        # Roll back only an agent this run brought into existence, never one it
        # merely added a version to.
        if not existed:
            print(f"\n{type(error).__name__}: {error}")
            try:
                project.agents.delete(args.agent_name)
                print(f"deleted agent {args.agent_name}")
            except Exception as cleanup_error:  # noqa: BLE001 — the original error matters more
                print(f"could not delete agent {args.agent_name}: {cleanup_error}")
        project.close()
        raise

    if args.delete:
        project.agents.delete(args.agent_name)
        print(f"deleted agent {args.agent_name}")
    project.close()


if __name__ == "__main__":
    main()
