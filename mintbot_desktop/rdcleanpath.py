"""RDCleanPath: the handshake a browser RDP client (IronRDP) makes with its proxy.

A browser cannot open a TLS connection of its own inside a WebSocket, so
IronRDP's web client leaves the first steps of an RDP connection to the
proxy it talks to: it sends one RDCleanPath PDU with the X.224 connection
request; the proxy opens TCP to the RDP server, forwards that request,
reads the server's X.224 confirm, upgrades its own side to TLS and answers
with a PDU carrying the confirm and the server's certificate chain. From
then on the WebSocket carries the RDP stream in the clear between browser
and proxy, and the proxy carries it inside TLS to the server. The gateway
in app.py is that proxy for the RDP server on this computer.

The PDU is ASN.1 DER (crates/ironrdp-rdcleanpath in IronRDP):

    RDCleanPathPdu ::= SEQUENCE {
        version              [0] EXPLICIT INTEGER (3390),
        error                [1] EXPLICIT RDCleanPathErr OPTIONAL,   -- proxy to client
        destination          [2] EXPLICIT UTF8String OPTIONAL,       -- client to proxy
        proxyAuth            [3] EXPLICIT UTF8String OPTIONAL,
        serverAuth           [4] EXPLICIT UTF8String OPTIONAL,
        preconnectionBlob    [5] EXPLICIT UTF8String OPTIONAL,
        x224ConnectionPdu    [6] EXPLICIT OCTET STRING OPTIONAL,     -- both ways
        serverCertChain      [7] EXPLICIT SEQUENCE OF OCTET STRING OPTIONAL,
        serverAddr           [9] EXPLICIT UTF8String OPTIONAL }
    RDCleanPathErr ::= SEQUENCE {
        errorCode            [0] EXPLICIT INTEGER,
        httpStatusCode       [1] EXPLICIT INTEGER OPTIONAL,
        wsaLastError         [2] EXPLICIT INTEGER OPTIONAL,
        tlsAlertCode         [3] EXPLICIT INTEGER OPTIONAL }

Only what this proxy needs of DER is here; the codec is strict about what
it reads and refuses the rest.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import ssl

VERSION = 3390

# Error codes, as IronRDP names them.
GENERAL_ERROR = 1
NEGOTIATION_ERROR = 2
TARGET_UNREACHABLE = 103
TLS_HANDSHAKE_FAILED = 105

# The security protocols an X.224 confirm may select (MS-RDPBCGR 2.2.1.2.1);
# all but the standard RDP security run inside TLS.
PROTOCOL_RDP, PROTOCOL_SSL, PROTOCOL_HYBRID, PROTOCOL_RDSTLS, PROTOCOL_HYBRID_EX = 0, 1, 2, 4, 8

TAG_INTEGER, TAG_OCTET_STRING, TAG_UTF8_STRING, TAG_SEQUENCE = 0x02, 0x04, 0x0C, 0x30


class DecodeError(ValueError):
    """Bytes that are not the PDU this module reads."""


# --- DER ------------------------------------------------------------------

def _length(count):
    if count < 0x80:
        return bytes([count])
    body = count.to_bytes((count.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, body):
    return bytes([tag]) + _length(len(body)) + body


def _integer(value):
    if value < 0:
        raise ValueError('only non-negative integers are encoded')
    body = value.to_bytes((value.bit_length() + 8) // 8, 'big')  # a leading zero keeps the sign positive
    return _tlv(TAG_INTEGER, body)


def _explicit(number, inner):
    """[number] EXPLICIT: a constructed context-specific tag around an encoded value."""
    return _tlv(0xA0 | number, inner)


def _read_tlv(data, offset):
    """(tag, body, next offset) of the value at `offset`."""
    if offset + 2 > len(data):
        raise DecodeError('truncated')
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise DecodeError('multi-byte tags are not used')
    first = data[offset + 1]
    offset += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or offset + count > len(data):
            raise DecodeError('bad length')
        length = int.from_bytes(data[offset:offset + count], 'big')
        if length < 0x80 or (count > 1 and length < 1 << (8 * (count - 1))):
            raise DecodeError('length is not minimal')
        offset += count
    if offset + length > len(data):
        raise DecodeError('truncated')
    return tag, data[offset:offset + length], offset + length


def _read_exactly(data, tag):
    found, body, end = _read_tlv(data, 0)
    if found != tag or end != len(data):
        raise DecodeError(f'expected tag {tag:#x}')
    return body


def _read_integer(data):
    body = _read_exactly(data, TAG_INTEGER)
    if not body or len(body) > 8 or body[0] & 0x80:
        raise DecodeError('bad integer')
    return int.from_bytes(body, 'big')


def _read_string(data):
    try:
        return _read_exactly(data, TAG_UTF8_STRING).decode('utf-8')
    except UnicodeDecodeError as error:
        raise DecodeError('bad string') from error


def _read_sequence(data):
    """The members of a SEQUENCE keyed by their explicit context-specific tag number."""
    body = _read_exactly(data, TAG_SEQUENCE)
    members, offset, last = {}, 0, -1
    while offset < len(body):
        tag, inner, offset = _read_tlv(body, offset)
        if tag & 0xE0 != 0xA0:
            raise DecodeError('a member without its context tag')
        number = tag & 0x1F
        if number <= last:
            raise DecodeError('members out of order')
        members[number] = inner
        last = number
    return members


# --- the PDU ------------------------------------------------------------

@dataclass(frozen=True)
class Error:
    error_code: int
    http_status_code: int | None = None
    wsa_last_error: int | None = None
    tls_alert_code: int | None = None

    def encode(self):
        parts = [_explicit(0, _integer(self.error_code))]
        for number, value in ((1, self.http_status_code), (2, self.wsa_last_error), (3, self.tls_alert_code)):
            if value is not None:
                parts.append(_explicit(number, _integer(value)))
        return _tlv(TAG_SEQUENCE, b''.join(parts))

    @classmethod
    def decode(cls, data):
        members = _read_sequence(data)
        if 0 not in members or set(members) - {0, 1, 2, 3}:
            raise DecodeError('bad error')
        return cls(_read_integer(members[0]), *(None if n not in members else _read_integer(members[n]) for n in (1, 2, 3)))


@dataclass(frozen=True)
class Pdu:
    version: int = VERSION
    error: Error | None = None
    destination: str | None = None
    proxy_auth: str | None = None
    server_auth: str | None = None
    preconnection_blob: str | None = None
    x224_connection_pdu: bytes | None = None
    server_cert_chain: list[bytes] | None = None
    server_addr: str | None = None

    def encode(self):
        parts = [_explicit(0, _integer(self.version))]
        if self.error is not None:
            parts.append(_explicit(1, self.error.encode()))
        for number, value in ((2, self.destination), (3, self.proxy_auth), (4, self.server_auth), (5, self.preconnection_blob)):
            if value is not None:
                parts.append(_explicit(number, _tlv(TAG_UTF8_STRING, value.encode('utf-8'))))
        if self.x224_connection_pdu is not None:
            parts.append(_explicit(6, _tlv(TAG_OCTET_STRING, self.x224_connection_pdu)))
        if self.server_cert_chain is not None:
            chain = b''.join(_tlv(TAG_OCTET_STRING, cert) for cert in self.server_cert_chain)
            parts.append(_explicit(7, _tlv(TAG_SEQUENCE, chain)))
        if self.server_addr is not None:
            parts.append(_explicit(9, _tlv(TAG_UTF8_STRING, self.server_addr.encode('utf-8'))))
        return _tlv(TAG_SEQUENCE, b''.join(parts))

    @classmethod
    def decode(cls, data):
        members = _read_sequence(data)
        if 0 not in members or set(members) - {0, 1, 2, 3, 4, 5, 6, 7, 9}:
            raise DecodeError('bad PDU')
        version = _read_integer(members[0])
        if version != VERSION:
            raise DecodeError(f'RDCleanPath version {version} is not {VERSION}')
        chain = None
        if 7 in members:
            chain, body, offset = [], _read_exactly(members[7], TAG_SEQUENCE), 0
            while offset < len(body):
                tag, cert, offset = _read_tlv(body, offset)
                if tag != TAG_OCTET_STRING:
                    raise DecodeError('bad certificate chain')
                chain.append(cert)
        strings = {n: _read_string(members[n]) for n in (2, 3, 4, 5, 9) if n in members}
        return cls(version, Error.decode(members[1]) if 1 in members else None,
                   strings.get(2), strings.get(3), strings.get(4), strings.get(5),
                   _read_exactly(members[6], TAG_OCTET_STRING) if 6 in members else None, chain, strings.get(9))


def pdu_length(data):
    """The whole PDU's length once its header is in `data`; None while the header is not yet complete.

    A client may hand the PDU over in more than one WebSocket message.
    """
    if len(data) < 2:
        return None
    if data[0] != TAG_SEQUENCE:
        raise DecodeError('not a PDU')
    first = data[1]
    if first < 0x80:
        return 2 + first
    count = first & 0x7F
    if count == 0 or count > 4:
        raise DecodeError('bad length')
    if len(data) < 2 + count:
        return None
    return 2 + count + int.from_bytes(data[2:2 + count], 'big')


def error_pdu(code, **details):
    return Pdu(error=Error(code, **details))


# --- X.224 ----------------------------------------------------------------

def tpkt_length(header):
    """The length of a TPKT (RFC 1006) whose first four bytes are `header`."""
    if len(header) < 4 or header[0] != 3:
        raise ValueError('not a TPKT')
    return int.from_bytes(header[2:4], 'big')


def selected_protocol(confirm):
    """The security protocol an X.224 Connection Confirm selects (MS-RDPBCGR 2.2.1.2).

    A confirm without a negotiation response selects standard RDP security;
    a negotiation failure is reported as such.
    """
    if len(confirm) < 11 or confirm[0] != 3 or confirm[5] != 0xD0:
        raise ValueError('not an X.224 connection confirm')
    body = confirm[11:]
    if not body:
        return PROTOCOL_RDP
    if len(body) < 8 or body[2:4] != b'\x08\x00':
        raise ValueError('malformed RDP negotiation response')
    if body[0] == 0x03:
        raise NegotiationFailure(int.from_bytes(body[4:8], 'little'))
    if body[0] != 0x02:
        raise ValueError('malformed RDP negotiation response')
    return int.from_bytes(body[4:8], 'little')


def rdp_pdu_length(data):
    """The length of the RDP PDU at the start of `data`, None while its header is incomplete, 0 when it is not one.

    Two framings share the stream (MS-RDPBCGR): TPKT (version byte 3,
    big-endian length in bytes 2 and 3) for X.224-carried PDUs, and
    fast-path (action bits 0, a one- or two-byte length in byte 1, and 2)
    for updates and input.
    """
    if len(data) < 2:
        return None
    if data[0] == 3:
        return None if len(data) < 4 else int.from_bytes(data[2:4], 'big')
    if data[0] & 0x03:
        return 0
    if data[1] & 0x80:
        return None if len(data) < 3 else ((data[1] & 0x7F) << 8) | data[2]
    return data[1]


def split_pdus(buffer):
    """The whole PDUs at the start of `buffer`, taken out of it; a stream that is not RDP is handed on as it is.

    IronRDP's client reads its connection sequence message by message and
    drops what follows the last PDU it waited for in the same WebSocket
    message: a server's finalization PDU and its first update, coalesced
    by TCP, left it waiting for an update it had already been sent. One
    PDU per message is what it expects of its proxy.
    """
    pdus = []
    while buffer:
        length = rdp_pdu_length(buffer)
        if length is None or length > len(buffer):
            break
        if length == 0:
            pdus.append(bytes(buffer))
            buffer.clear()
            break
        pdus.append(bytes(buffer[:length]))
        del buffer[:length]
    return pdus


class NegotiationFailure(Exception):
    def __init__(self, code):
        super().__init__(f'the RDP server refused the connection (negotiation failure {code})')
        self.code = code


# --- the proxy's part ---------------------------------------------------

@dataclass(frozen=True)
class Target:
    """The RDP server the proxy opens: where, and the SHA-256 of the certificate it must show (None: any)."""
    host: str
    port: int
    fingerprint: str | None = None


@dataclass
class Opened:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    response: Pdu
    certificate: bytes | None = field(default=None)


class ProxyError(Exception):
    """Why the connection was not opened, with the RDCleanPath error to tell the client."""
    def __init__(self, message, code, **details):
        super().__init__(message)
        self.pdu = error_pdu(code, **details)


async def open_connection(target, request, timeout=10.0):
    """Open the RDP server for a client's request PDU: TCP, X.224, TLS; the server's confirm in the response."""
    if request.x224_connection_pdu is None:
        raise ProxyError('the request carries no X.224 connection request', GENERAL_ERROR)
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(target.host, target.port)
    except (OSError, asyncio.TimeoutError) as error:
        raise ProxyError(f'the RDP server at {target.host}:{target.port} is unreachable: {error}', TARGET_UNREACHABLE) from error
    try:
        async with asyncio.timeout(timeout):
            writer.write(request.x224_connection_pdu)
            await writer.drain()
            header = await reader.readexactly(4)
            confirm = header + await reader.readexactly(tpkt_length(header) - 4)
            try:
                protocol = selected_protocol(confirm)
            except NegotiationFailure as failure:
                raise ProxyError(str(failure), NEGOTIATION_ERROR) from failure
            certificate = None
            if protocol != PROTOCOL_RDP:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                try:
                    await writer.start_tls(context)
                except (ssl.SSLError, OSError) as error:
                    raise ProxyError(f'TLS with the RDP server failed: {error}', TLS_HANDSHAKE_FAILED) from error
                certificate = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
                digest = hashlib.sha256(certificate).hexdigest()
                if target.fingerprint and digest != target.fingerprint.lower():
                    raise ProxyError('the RDP server showed a certificate other than the one it is known by', TLS_HANDSHAKE_FAILED)
    except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
        writer.close()
        raise ProxyError(f'the RDP server did not complete the connection: {error}', TARGET_UNREACHABLE) from error
    except ProxyError:
        writer.close()
        raise
    response = Pdu(x224_connection_pdu=confirm, server_cert_chain=[certificate] if certificate else [], server_addr=target.host)
    return Opened(reader, writer, response, certificate)
