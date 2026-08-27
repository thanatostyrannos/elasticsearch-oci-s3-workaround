"""Where credentials come from, and the two things that must never happen.

THE PATH GOES ON THE COMMAND LINE, THE SECRET GOES IN A FILE. Anything in argv
is visible in `ps` to every user on the host, lands in shell history, gets
copied into container specs and Kubernetes manifests, and turns up in CI logs
and crash dumps. So `--credentials FILE` is a flag and `--secret-key VALUE`
never will be. A flag that names a credential takes a path, never a value.

A SECRET NEVER REACHES AN OUTPUT STREAM. Not a log, not an error message, not
the report, not a traceback. `Secret` below makes that structural rather than
a rule people remember: interpolating one anywhere produces `<secret>`, and
the only way to the value is `reveal()`, which appears at exactly two call
sites, both of them a signing step.

WHERE IT LOOKS, AND WHY IN THAT ORDER.

  1. `--credentials FILE`, the explicit override, for an operator who wants
     one file or is somewhere the standard config does not exist.
  2. The standard location the operator already has: `~/.aws/credentials` for
     the S3 compatibility path, `~/.oci/config` for the OCI native path.
     Reading these matters. Forcing a new combined file creates a SECOND copy
     of the credential, and the second copy is the one nobody rotates and
     nobody deletes.
  3. The environment. Supported because CI needs it, not recommended, and the
     documentation says why: `/proc/<pid>/environ` is readable by the same
     user and a container's environment shows up in an inspect.

NO CREDENTIAL AT ALL IS A SUPPORTED WAY TO RUN. `--local-repo` against a
mirror, with corroboration not requested, needs none of this. That is the
offline and jump-host case and it is one of the reasons this tool runs where
the older ones could not. Nothing here may quietly make a config file
mandatory for it.

ABSENCE IS NOT A FALLBACK TO ANONYMOUS. A credentials file that does not carry
the section for the transport being used is a refusal. So is a section with an
empty value. A malformed credential must not degrade into an unauthenticated
request that fails somewhere less obvious, which is how a permissions problem
comes to look like a missing bucket.
"""

from __future__ import annotations

import configparser
import json
import os
import stat
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from .errors import GenerationChainError

AWS_CREDENTIALS_FILE = "~/.aws/credentials"
OCI_CONFIG_FILE = "~/.oci/config"
# Better than argv, worse than a file. Supported for CI, not recommended.
S3_ACCESS_ENV = "AWS_ACCESS_KEY_ID"
S3_SECRET_ENV = "AWS_SECRET_ACCESS_KEY"
ES_USERNAME_ENV = "GENCHAIN_ES_USERNAME"
ES_PASSWORD_ENV = "GENCHAIN_ES_PASSWORD"
ES_API_KEY_ENV = "GENCHAIN_ES_API_KEY"

GROUP_AND_WORLD = stat.S_IRWXG | stat.S_IRWXO

CREDENTIAL_SUMMARY = (
    "Credentials: none at all is a supported way to run, with --local-repo "
    "against a mirror and no corroboration requested. The store credential is "
    "needed for the s3 and oci transports. The Elasticsearch credential is "
    "needed only when --elasticsearch is passed. Secrets are read from the "
    "JSON file named by --credentials, or from the standard locations "
    "(~/.aws/credentials, ~/.oci/config), or from the environment. Never from "
    "the command line: argv is visible in ps to every user on the host. A "
    "credentials file other users can read is refused rather than used."
)


class CredentialError(GenerationChainError):
    """A credential is missing, unusable or unsafely stored, named precisely."""


class Secret:
    """A value that cannot be printed by accident.

    Every path out of this object except `reveal()` produces `<secret>`, so an
    f-string in an error message, a repr in a debugger, a dataclass repr and a
    JSON dump all render the placeholder. The two `reveal()` call sites are
    both a signing step, which makes them easy to audit.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "<secret>"

    def __str__(self) -> str:
        return "<secret>"

    def __format__(self, spec: str) -> str:
        return "<secret>"


def as_secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(str(value))


@dataclass(frozen=True)
class CredentialFile:
    """One JSON file holding the sections this tool understands.

    Deliberately small:

        {"s3": {"access_key_id": "...", "secret_access_key": "..."},
         "oci": {"tenancy": "...", "user": "...", "fingerprint": "...",
                 "key_file": "/path/to/key.pem", "pass_phrase": null},
         "elasticsearch": {"api_key": "..."}}

    `elasticsearch` takes either `api_key` or `username` and `password`.
    """

    path: str
    sections: Mapping[str, Mapping[str, Any]]

    @classmethod
    def read(cls, path: str) -> "CredentialFile":
        resolved = os.path.expanduser(path)
        require_private(resolved)
        try:
            with open(resolved, encoding="utf-8") as handle:
                document = json.load(handle)
        except OSError as exc:
            raise CredentialError(
                f"cannot read the credentials file {resolved}: {exc.strerror}"
            ) from None
        except json.JSONDecodeError as exc:
            raise CredentialError(
                f"the credentials file {resolved} is not JSON: {exc}") from None
        if not isinstance(document, dict):
            raise CredentialError(
                f"the credentials file {resolved} is not an object")
        return cls(path=resolved, sections={
            name: body for name, body in document.items()
            if isinstance(name, str) and isinstance(body, dict)})

    def section(self, name: str) -> Mapping[str, Any]:
        body = self.sections.get(name)
        if body is None:
            raise CredentialError(
                f"the credentials file {self.path} has no {name!r} section. A "
                "missing section is a refusal rather than a fall back to an "
                "unauthenticated request")
        return body

    def required(self, section: str, field: str) -> str:
        value = self.section(section).get(field)
        if not isinstance(value, str) or not value.strip():
            raise CredentialError(
                f"the {section!r} section of {self.path} has no usable "
                f"{field!r}")
        return value


def require_private(path: str) -> None:
    """Refuse a credential the rest of the machine can read.

    A file that arrived by `scp` lands at 0644 by default, and that is the
    common way a credential leaks on a shared host. The message names the file,
    the mode found and the mode required, because a refusal that only
    complains costs an hour and one that names the remedy costs a minute.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise CredentialError(
            f"cannot read {path}: {exc.strerror}") from None
    if mode & GROUP_AND_WORLD:
        raise CredentialError(
            f"{path} is mode {stat.S_IMODE(mode):04o} and must be 0600 or "
            f"0400. Any other mode lets other users on this host read a "
            f"credential. Run `chmod 600 {path}` and try again. Nothing was "
            f"read")


def load_s3(explicit: Optional[str], profile: str = "default"):
    """Access key and secret for the S3 compatibility path."""
    from .sources.s3 import S3Credentials
    if explicit:
        credentials = CredentialFile.read(explicit)
        return S3Credentials(
            credentials.required("s3", "access_key_id"),
            Secret(credentials.required("s3", "secret_access_key")))
    standard = os.path.expanduser(AWS_CREDENTIALS_FILE)
    if os.path.exists(standard):
        return _from_aws_file(standard, profile)
    access = os.environ.get(S3_ACCESS_ENV)
    secret = os.environ.get(S3_SECRET_ENV)
    if access and secret:
        return S3Credentials(access, Secret(secret))
    raise CredentialError(
        "no store credential found. Looked at --credentials, then "
        f"{AWS_CREDENTIALS_FILE}, then {S3_ACCESS_ENV} and {S3_SECRET_ENV} in "
        "the environment. This tool never takes a secret on the command line")


def _from_aws_file(path: str, profile: str):
    from .sources.s3 import S3Credentials
    require_private(path)
    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise CredentialError(f"cannot parse {path}")
    if profile not in parser:
        raise CredentialError(f"{path} has no profile named {profile!r}")
    section = parser[profile]
    access = section.get("aws_access_key_id", "").strip()
    secret = section.get("aws_secret_access_key", "").strip()
    if not access or not secret:
        raise CredentialError(
            f"profile {profile!r} in {path} is missing aws_access_key_id or "
            "aws_secret_access_key")
    return S3Credentials(access, Secret(secret))


def load_oci(explicit: Optional[str], profile: str = "DEFAULT"):
    """Key id and private key for the OCI native path."""
    from .sources.oci import OciCredentials
    from .sources.signing.rsa import RsaPrivateKey
    if explicit:
        credentials = CredentialFile.read(explicit)
        key_path = os.path.expanduser(credentials.required("oci", "key_file"))
        require_private(key_path)
        with open(key_path, "rb") as handle:
            pem = handle.read()
        return OciCredentials(
            key_id="{}/{}/{}".format(credentials.required("oci", "tenancy"),
                                     credentials.required("oci", "user"),
                                     credentials.required("oci", "fingerprint")),
            private_key=RsaPrivateKey.from_pem(pem))
    return OciCredentials.from_profile(None, profile)


def load_elasticsearch(explicit: Optional[str]):
    """The cluster credential, needed ONLY when corroboration is requested."""
    from .corroboration import Credentials
    if explicit:
        credentials = CredentialFile.read(explicit)
        section = credentials.section("elasticsearch")
        if "api_key" in section:
            return Credentials(
                api_key=Secret(credentials.required("elasticsearch", "api_key")))
        return Credentials(
            username=credentials.required("elasticsearch", "username"),
            password=Secret(credentials.required("elasticsearch", "password")))
    api_key = os.environ.get(ES_API_KEY_ENV)
    if api_key:
        return Credentials(api_key=Secret(api_key))
    username = os.environ.get(ES_USERNAME_ENV)
    password = os.environ.get(ES_PASSWORD_ENV)
    if username and password:
        return Credentials(username=username, password=Secret(password))
    raise CredentialError(
        "corroboration was requested and no cluster credential was found. "
        "Put an 'elasticsearch' section in the file named by --credentials, "
        f"or set {ES_API_KEY_ENV}, or {ES_USERNAME_ENV} and "
        f"{ES_PASSWORD_ENV}. This tool never takes a secret on the command "
        "line")
