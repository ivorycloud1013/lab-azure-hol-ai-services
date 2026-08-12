#!/usr/bin/env python3

import argparse

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, MCPToolFilter, PromptAgentDefinition
from azure.core.exceptions import ResourceNotFoundError

import identity

INSTRUCTIONS = (
    "You answer questions by calling the MCP tools attached to you and from nothing else. "
    "List the tools you have before assuming something is out of reach, and name the tool and "
    "the source each fact came from. "
    "Say you could not find it rather than guessing. Answer in the language of the question."
)

DEFAULT_AGENT_NAME = "hol-mcp-ops"

# Microsoft Learn serves a public MCP endpoint — nothing to deploy, no credential to
# present, and the service reaches it over the internet rather than through this
# lab's network. It is the one server every run of this script can count on.
LEARN_MCP_URL = "https://learn.microsoft.com/api/mcp"
LEARN_DESCRIPTION = "Microsoft Learn — official Azure and Microsoft product documentation"

# Every tool call would otherwise stop and wait for a human. A lab answering its own
# questions on the terminal has nobody to ask, and Learn is read-only.
APPROVAL_NEVER = "never"

MCP_OUTPUT_TYPES = ("mcp_list_tools", "mcp_call", "mcp_approval_request")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Attach remote MCP servers to a Foundry prompt agent and ask it questions.",
        epilog="--learn is the only server this lab reaches with no setup at all — it is public "
               "and unauthenticated. --mcp with an audience hands the agent your own Entra token "
               "instead, so you need the role on the target yourself and the version stops "
               "working when that token expires. --connection points at a connection the project "
               "already holds, which authenticates as the project identity and keeps working — "
               "mix the three freely. --endpoint is the project endpoint, "
               "https://<resource>.ai.azure.com/api/projects/<project>.",
    )
    parser.add_argument("--endpoint", required=True, help="Foundry project endpoint")
    parser.add_argument("--deployment", default="gpt-5.6-terra", help="model deployment name")

    identity.add_auth_arguments(parser)

    servers = parser.add_argument_group("MCP servers")
    servers.add_argument("--learn", action="store_true",
                         help="attach the public Microsoft Learn MCP server")
    servers.add_argument("--mcp", action="append", default=[], metavar="LABEL=URL[=AUDIENCE]",
                         help="any other remote MCP server, unauthenticated without an audience")
    servers.add_argument("--connection", action="append", default=[], metavar="LABEL=CONNECTION_ID",
                         help="MCP server the project already has a connection for — "
                              "the connection carries the authentication, so no audience")

    parser.add_argument("--allowed-tool", action="append", default=[], metavar="NAME",
                        help="restrict every server to these tool names, repeat for more")
    parser.add_argument("--read-only", action="store_true",
                        help="restrict every server to tools it annotates as read-only")
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
    if args.allowed_tool and args.read_only:
        parser.error("--allowed-tool names tools and --read-only filters them, pass only one")
    if not (args.learn or args.mcp or args.connection):
        parser.error("attach at least one server with --learn, --mcp or --connection")
    return args


def split_option(value, option, shape):
    """Split an A=B style option, so a missing half fails here and not at the service."""
    parts = shape.split("=")
    fields = value.split("=", len(parts) - 1)
    if len(fields) != len(parts) or not all(fields):
        raise SystemExit(f"{option} {value} must be {shape}")
    return fields


def learn_spec():
    """No audience, because the server asks for no credential."""
    return {"label": "learn", "url": LEARN_MCP_URL, "audience": None,
            "description": LEARN_DESCRIPTION}


def mcp_specs(values):
    """LABEL=URL, or LABEL=URL=AUDIENCE when the server wants an Entra token."""
    specs = []
    for value in values:
        fields = value.split("=", 2)
        if len(fields) < 2 or not all(fields):
            raise SystemExit(f"--mcp {value} must be LABEL=URL or LABEL=URL=AUDIENCE")
        specs.append({"label": fields[0], "url": fields[1], "description": None,
                      "audience": fields[2] if len(fields) > 2 else None})
    return specs


def connection_specs(values):
    return [
        dict(zip(("label", "connection_id"), split_option(value, "--connection", "LABEL=CONNECTION_ID")))
        for value in values
    ]


def build_specs(args):
    """One flat list of servers, whichever option each of them arrived on."""
    return (([learn_spec()] if args.learn else [])
            + mcp_specs(args.mcp) + connection_specs(args.connection))


def get_tool_filter(args):
    if args.allowed_tool:
        return args.allowed_tool
    if args.read_only:
        # Only servers that annotate their tools with readOnlyHint match this.
        return MCPToolFilter(read_only=True)
    return None


def get_token(credential, audience, cache):
    """One token per audience — several servers can share one."""
    if audience not in cache:
        cache[audience] = credential.get_token(f"{audience.rstrip('/')}/.default").token
    return cache[audience]


def build_tool(spec, allowed_tools, credential, tokens):
    """Declare one server as a tool on the agent.

    The service calls the server itself, so what it needs is a URL it can reach and
    a credential to present. Nothing is proxied through this process.
    """
    common = {"server_label": spec["label"], "require_approval": APPROVAL_NEVER,
              "allowed_tools": allowed_tools}

    if spec.get("connection_id"):
        # The connection holds the credential, so the agent authenticates as the
        # project identity and this version keeps working after today.
        return MCPTool(**common, project_connection_id=spec["connection_id"])

    optional = {}
    if spec.get("description"):
        optional["server_description"] = spec["description"]
    if spec.get("audience"):
        optional["authorization"] = get_token(credential, spec["audience"], tokens)
    return MCPTool(**common, server_url=spec["url"], **optional)


def build_tools(specs, allowed_tools, credential):
    tokens, tools = {}, []
    for spec in specs:
        tools.append(build_tool(spec, allowed_tools, credential, tokens))
        print(f"attached {spec['label']} — {spec.get('url') or spec['connection_id']}")
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
            tools=build_tools(specs, get_tool_filter(args), credential),
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
    specs = build_specs(args)
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
