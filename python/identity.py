"""Authentication for Foundry calls. Shared by the scripts under python/.

The Foundry accounts in this lab are keyless (disableLocalAuth=true), so the default
path is an Entra ID token. api-key / access-token build no credential object — the
caller passes the secret to the SDK directly.
"""

import os

from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    DeviceCodeCredential,
    EnvironmentCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)

# Scope for the v1 API. The classic data-plane scope was
# https://cognitiveservices.azure.com/.default — swap it back if a call returns 401.
SCOPE = "https://ai.azure.com/.default"

AUTH_METHODS = [
    "default",             # az login / environment variables / managed identity, auto-detected
    "cli",                 # az login session
    "device-code",         # terminal without a browser
    "environment",         # AZURE_* environment variables
    "managed-identity",    # jumpbox VM
    "client-secret",       # service principal + secret
    "client-certificate",  # service principal + certificate
    "api-key",             # API key (401 on a keyless account)
    "access-token",        # token you already acquired
]


def add_auth_arguments(parser):
    """Add the auth arguments. Defaults come from the standard AZURE_* variables."""
    group = parser.add_argument_group("authentication")
    group.add_argument("--auth", choices=AUTH_METHODS, default="default")
    group.add_argument("--tenant-id", default=os.getenv("AZURE_TENANT_ID"))
    group.add_argument("--client-id", default=os.getenv("AZURE_CLIENT_ID"))
    group.add_argument("--certificate-path", default=os.getenv("AZURE_CLIENT_CERTIFICATE_PATH"))
    group.add_argument("--client-secret", default=os.getenv("AZURE_CLIENT_SECRET"))
    group.add_argument("--api-key", default=os.getenv("AZURE_OPENAI_API_KEY"))
    group.add_argument("--access-token", default=os.getenv("AZURE_OPENAI_ACCESS_TOKEN"))
    return group


def get_credential(args):
    """Build the credential for --auth. Works with any azure-* SDK."""
    if args.auth == "cli":
        return AzureCliCredential()
    if args.auth == "device-code":
        return DeviceCodeCredential(tenant_id=args.tenant_id, client_id=args.client_id)
    if args.auth == "environment":
        return EnvironmentCredential()
    if args.auth == "managed-identity":
        # Without a client_id this picks the system-assigned identity.
        return ManagedIdentityCredential(client_id=args.client_id)
    if args.auth == "client-secret":
        return ClientSecretCredential(args.tenant_id, args.client_id, args.client_secret)
    if args.auth == "client-certificate":
        return CertificateCredential(args.tenant_id, args.client_id, args.certificate_path)
    return DefaultAzureCredential()


def get_token_provider(args):
    """Token provider for the openai SDK's azure_ad_token_provider."""
    return get_bearer_token_provider(get_credential(args), SCOPE)
