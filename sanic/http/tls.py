from __future__ import annotations

import os
import ssl
import subprocess
import sys

from contextlib import suppress
from pathlib import Path
from ssl import SSLContext
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Union

from sanic.application.constants import Mode
from sanic.application.spinner import loading
from sanic.constants import DEFAULT_LOCAL_TLS_CERT, DEFAULT_LOCAL_TLS_KEY
from sanic.exceptions import SanicException
from sanic.helpers import Default
from sanic.log import logger


if TYPE_CHECKING:
    from sanic import Sanic


# Only allow secure ciphers, notably leaving out AES-CBC mode
# OpenSSL chooses ECDSA or RSA depending on the cert in use
CIPHERS_TLS12 = [
    "ECDHE-ECDSA-CHACHA20-POLY1305",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES128-GCM-SHA256",
]


def create_context(
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
    password: Optional[str] = None,
) -> ssl.SSLContext:
    """Create a context with secure crypto and HTTP/1.1 in protocols."""
    context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(":".join(CIPHERS_TLS12))
    context.set_alpn_protocols(["http/1.1"])
    context.sni_callback = server_name_callback
    if certfile and keyfile:
        context.load_cert_chain(certfile, keyfile, password)
    return context


def shorthand_to_ctx(
    ctxdef: Union[None, ssl.SSLContext, dict, str]
) -> Optional[ssl.SSLContext]:
    """Convert an ssl argument shorthand to an SSLContext object."""
    if ctxdef is None or isinstance(ctxdef, ssl.SSLContext):
        return ctxdef
    if isinstance(ctxdef, str):
        return load_cert_dir(ctxdef)
    if isinstance(ctxdef, dict):
        return CertSimple(**ctxdef)
    raise ValueError(
        f"Invalid ssl argument {type(ctxdef)}."
        " Expecting a list of certdirs, a dict or an SSLContext."
    )


def process_to_context(
    ssldef: Union[None, ssl.SSLContext, dict, str, list, tuple]
) -> Optional[ssl.SSLContext]:
    """Process app.run ssl argument from easy formats to full SSLContext."""
    return (
        CertSelector(map(shorthand_to_ctx, ssldef))
        if isinstance(ssldef, (list, tuple))
        else shorthand_to_ctx(ssldef)
    )


def load_cert_dir(p: str) -> ssl.SSLContext:
    if os.path.isfile(p):
        raise ValueError(f"Certificate folder expected but {p} is a file.")
    keyfile = os.path.join(p, "privkey.pem")
    certfile = os.path.join(p, "fullchain.pem")
    if not os.access(keyfile, os.R_OK):
        raise ValueError(
            f"Certificate not found or permission denied {keyfile}"
        )
    if not os.access(certfile, os.R_OK):
        raise ValueError(
            f"Certificate not found or permission denied {certfile}"
        )
    return CertSimple(certfile, keyfile)


class CertSimple(ssl.SSLContext):
    """A wrapper for creating SSLContext with a sanic attribute."""

    sanic: Dict[str, Any]

    def __new__(cls, cert, key, **kw):
        # try common aliases, rename to cert/key
        certfile = kw["cert"] = kw.pop("certificate", None) or cert
        keyfile = kw["key"] = kw.pop("keyfile", None) or key
        password = kw.pop("password", None)
        if not certfile or not keyfile:
            raise ValueError("SSL dict needs filenames for cert and key.")
        subject = {}
        if "names" not in kw:
            cert = ssl._ssl._test_decode_cert(certfile)  # type: ignore
            kw["names"] = [
                name
                for t, name in cert["subjectAltName"]
                if t in ["DNS", "IP Address"]
            ]
            subject = {k: v for item in cert["subject"] for k, v in item}
        self = create_context(certfile, keyfile, password)
        self.__class__ = cls
        self.sanic = {**subject, **kw}
        return self

    def __init__(self, cert, key, **kw):
        pass  # Do not call super().__init__ because it is already initialized


class CertSelector(ssl.SSLContext):
    """Automatically select SSL certificate based on the hostname that the
    client is trying to access, via SSL SNI. Paths to certificate folders
    with privkey.pem and fullchain.pem in them should be provided, and
    will be matched in the order given whenever there is a new connection.
    """

    def __new__(cls, ctxs):
        return super().__new__(cls)

    def __init__(self, ctxs: Iterable[Optional[ssl.SSLContext]]):
        super().__init__()
        self.sni_callback = selector_sni_callback  # type: ignore
        self.sanic_select = []
        self.sanic_fallback = None
        all_names = []
        for i, ctx in enumerate(ctxs):
            if not ctx:
                continue
            names = dict(getattr(ctx, "sanic", {})).get("names", [])
            all_names += names
            self.sanic_select.append(ctx)
            if i == 0:
                self.sanic_fallback = ctx
        if not all_names:
            raise ValueError(
                "No certificates with SubjectAlternativeNames found."
            )
        logger.info(f"Certificate vhosts: {', '.join(all_names)}")


def find_cert(self: CertSelector, server_name: str):
    """Find the first certificate that matches the given SNI.

    :raises ssl.CertificateError: No matching certificate found.
    :return: A matching ssl.SSLContext object if found."""
    if not server_name:
        if self.sanic_fallback:
            return self.sanic_fallback
        raise ValueError(
            "The client provided no SNI to match for certificate."
        )
    for ctx in self.sanic_select:
        if match_hostname(ctx, server_name):
            return ctx
    if self.sanic_fallback:
        return self.sanic_fallback
    raise ValueError(f"No certificate found matching hostname {server_name!r}")


def match_hostname(
    ctx: Union[ssl.SSLContext, CertSelector], hostname: str
) -> bool:
    """Match names from CertSelector against a received hostname."""
    # Local certs are considered trusted, so this can be less pedantic
    # and thus faster than the deprecated ssl.match_hostname function is.
    names = dict(getattr(ctx, "sanic", {})).get("names", [])
    hostname = hostname.lower()
    for name in names:
        if name.startswith("*."):
            if hostname.split(".", 1)[-1] == name[2:]:
                return True
        elif name == hostname:
            return True
    return False


def selector_sni_callback(
    sslobj: ssl.SSLObject, server_name: str, ctx: CertSelector
) -> Optional[int]:
    """Select a certificate matching the SNI."""
    # Call server_name_callback to store the SNI on sslobj
    server_name_callback(sslobj, server_name, ctx)
    # Find a new context matching the hostname
    try:
        sslobj.context = find_cert(ctx, server_name)
    except ValueError as e:
        logger.warning(f"Rejecting TLS connection: {e}")
        # This would show ERR_SSL_UNRECOGNIZED_NAME_ALERT on client side if
        # asyncio/uvloop did proper SSL shutdown. They don't.
        return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
    return None  # mypy complains without explicit return


def server_name_callback(
    sslobj: ssl.SSLObject, server_name: str, ctx: ssl.SSLContext
) -> None:
    """Store the received SNI as sslobj.sanic_server_name."""
    sslobj.sanic_server_name = server_name  # type: ignore


def _make_path(maybe_path: Union[Path, str], tmpdir: Optional[Path]) -> Path:
    if isinstance(maybe_path, Path):
        return maybe_path
    else:
        path = Path(maybe_path)
        if not path.exists():
            if not tmpdir:
                raise RuntimeError("Reached an unknown state. No tmpdir.")
            return tmpdir / maybe_path

    return path


def get_ssl_context(app: Sanic, ssl: Optional[SSLContext]) -> SSLContext:
    if ssl:
        return ssl

    if app.state.mode is Mode.PRODUCTION:
        raise SanicException(
            "Cannot run Sanic as an HTTPS server in PRODUCTION mode "
            "without passing a TLS certificate. If you are developing "
            "locally, please enable DEVELOPMENT mode and Sanic will "
            "generate a localhost TLS certificate. For more information "
            "please see: ___."
        )

    try:
        tmpdir = None
        if isinstance(app.config.LOCAL_TLS_KEY, Default) or isinstance(
            app.config.LOCAL_TLS_CERT, Default
        ):
            tmpdir = Path(mkdtemp())

        key = (
            DEFAULT_LOCAL_TLS_KEY
            if isinstance(app.config.LOCAL_TLS_KEY, Default)
            else app.config.LOCAL_TLS_KEY
        )
        cert = (
            DEFAULT_LOCAL_TLS_CERT
            if isinstance(app.config.LOCAL_TLS_CERT, Default)
            else app.config.LOCAL_TLS_CERT
        )

        key_path = _make_path(key, tmpdir)
        cert_path = _make_path(cert, tmpdir)

        if not cert_path.exists():
            generate_local_certificate(
                key_path, cert_path, app.config.LOCALHOST
            )
    finally:

        @app.main_process_stop
        async def cleanup(*_):
            if tmpdir:
                with suppress(FileNotFoundError):
                    key_path.unlink()
                    cert_path.unlink()
                tmpdir.rmdir()

    return CertSimple(cert_path, key_path)


def generate_local_certificate(
    key_path: Path, cert_path: Path, localhost: str
):
    check_mkcert()

    if not key_path.parent.exists() or not cert_path.parent.exists():
        raise SanicException(
            f"Cannot generate certificate at [{key_path}, {cert_path}]. One "
            "or more of the directories does not exist."
        )

    message = "Generating TLS certificate"
    with loading(message):
        cmd = [
            "mkcert",
            "-key-file",
            str(key_path),
            "-cert-file",
            str(cert_path),
            localhost,
        ]
        resp = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    sys.stdout.write("\r" + " " * (len(message) + 4))
    sys.stdout.flush()
    sys.stdout.write(resp.stdout)


def check_mkcert():
    try:
        subprocess.run(
            ["mkcert", "-help"],
            check=True,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    except Exception as e:
        raise SanicException(
            "Sanic uses mkcert to generate local TLS certificates. Since you "
            "did not supply a certificate, Sanic is attempting to generate "
            "one for you, but cannot proceed since mkcert does not appear to "
            "be installed. Please install mkcert or supply TLS certificates "
            "to proceed. Installation instructions can be found here: "
            "https://github.com/FiloSottile/mkcert"
        ) from e
