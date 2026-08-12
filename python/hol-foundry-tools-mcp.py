#!/usr/bin/env python3

import argparse

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError

import identity

INSTRUCTIONS = (
    "You answer questions by calling the MCP tools attached to you and from nothing else. "
    "List the tools you have before assuming something is out of reach, and name the tool and "
    "the source each fact came from. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

DEFAULT_AGENT_NAME = "hol-mcp-ops"

# Every tool call would otherwise stop and wait for a human. A lab answering its own
# questions on the terminal has nobody to ask, so pair this with read-only servers.
APPROVAL_NEVER = "never"

MCP_OUTPUT_TYPES = ("mcp_list_tools", "mcp_call", "mcp_approval_request")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Attach remote MCP servers to a Foundry prompt agent and ask it questions.",
        epilog="--mcp without an audience is the server this lab reaches with no setup at all, "
               "e.g. --mcp learn=https://learn.microsoft.com/api/mcp, which is public and "
               "unauthenticated. --mcp with an audience hands the agent your own Entra token "
               "instead, so you need the role on the target yourself and the version stops "
               "working when that token expires. --endpoint is the project endpoint, "
               "https://<resource>.ai.azure.com/api/projects/<project>.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    parser.add_argument("--mcp", action="append", default=[], required=True,
                        metavar="LABEL=URL[=AUDIENCE]",
                        help="remote MCP server, unauthenticated without an audience, "
                             "e.g. learn=https://learn.microsoft.com/api/mcp, repeat for more")
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME,
                        help=f"agent to create a version of (default {DEFAULT_AGENT_NAME})")
    parser.add_argument("--question", action="append", default=[], required=True, metavar="TEXT",
                        help="what to ask, repeat to follow up in the same conversation")
    parser.add_argument("--show-tools", action="store_true",
                        help="print the tools the agent discovered and every call it makes")
    parser.add_argument("--delete", action="store_true", help="delete the agent when done")

    args = parser.parse_args()
    if args.auth in ("api-key", "access-token"):
        parser.error(f"--auth {args.auth} is not supported by the projects SDK, use another method")
    return args


def mcp_specs(values):
    """LABEL=URL, or LABEL=URL=AUDIENCE when the server wants an Entra token."""
    specs = []
    for value in values:
        fields = value.split("=", 2)
        if len(fields) < 2 or not all(fields):
            raise SystemExit(f"--mcp {value} must be LABEL=URL or LABEL=URL=AUDIENCE")
        specs.append({"label": fields[0], "url": fields[1],
                      "audience": fields[2] if len(fields) > 2 else None})
    return specs


def get_token(credential, audience, cache):
    """One token per audience — several servers can share one."""
    if audience not in cache:
        cache[audience] = credential.get_token(f"{audience.rstrip('/')}/.default").token
    return cache[audience]


def build_tool(spec, credential, tokens):
    """Declare one server as a tool on the agent.

    The service calls the server itself, so what it needs is a URL it can reach and
    a credential to present. Nothing is proxied through this process.
    """
    optional = {}
    if spec["audience"]:
        optional["authorization"] = get_token(credential, spec["audience"], tokens)
    return MCPTool(server_label=spec["label"], server_url=spec["url"],
                   require_approval=APPROVAL_NEVER, **optional)


def build_tools(specs, credential):
    tokens, tools = {}, []
    for spec in specs:
        tools.append(build_tool(spec, credential, tokens))
        print(f"attached {spec['label']} — {spec['url']}")
    return tools


def create_version(project, args, specs, credential):
    """A prompt agent is declarative — model, instructions and servers live in the
    service, and it calls the servers itself. Nothing runs in this process.

    A new version every run, because a server given an audience has a bearer token
    written into the definition and that token expires within the hour. Versions are
    cheap, and the alternative is an agent that starts failing on its second day.
    """
    agent = project.agents.create_version(
        agent_name=args.agent_name,
        definition=PromptAgentDefinition(
            model=args.deployment,
            instructions=INSTRUCTIONS,
            tools=build_tools(specs, credential),
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


def print_tool_activity(response):
    """MCP work shows up in the output as its own items, next to the answer."""
    for item in response.output:
        if item.type not in MCP_OUTPUT_TYPES:
            continue
        if item.type == "mcp_list_tools":
            names = [tool.name for tool in getattr(item, "tools", None) or []]
            print(f"  [{getattr(item, 'server_label', '?')} offers {', '.join(names) or 'nothing'}]")
            continue
        error = getattr(item, "error", None)
        print(f"  [{getattr(item, 'server_label', '?')}.{getattr(item, 'name', item.type)}"
              f"{' failed: ' + str(error) if error else ''}]")


def ask(client, conversation_id, question, show_tools):
    print(f"\n> {question}")
    response = client.responses.create(conversation=conversation_id, input=question)
    if show_tools:
        print_tool_activity(response)
    print(response.output_text)


def main():
    args = parse_args()
    # Before the first network call, so a malformed --mcp fails on the spot.
    specs = mcp_specs(args.mcp)
    credential = identity.get_credential(args)
    project = AIProjectClient(endpoint=args.endpoint, credential=credential)

    existed = agent_exists(project, args.agent_name)
    try:
        agent = create_version(project, args, specs, credential)
        # Same client bound to the agent, so responses run through its tools.
        agent_client = project.get_openai_client(agent_name=agent.name)
        conversation = agent_client.conversations.create()
        for question in args.question:
            ask(agent_client, conversation.id, question, args.show_tools)
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
