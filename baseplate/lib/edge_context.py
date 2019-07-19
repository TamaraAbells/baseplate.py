import collections
import logging

import jwt

from thrift import TSerialization
from thrift.protocol.TBinaryProtocol import TBinaryProtocolAcceleratedFactory

from baseplate.lib import cached_property


logger = logging.getLogger(__name__)


class NoAuthenticationError(Exception):
    """Raised when trying to use an invalid or missing authentication token."""


class AuthenticationTokenValidator:
    """Factory that knows how to validate raw authentication tokens."""

    def __init__(self, secrets):
        self.secrets = secrets

    def validate(self, token):
        """Validate a raw authentication token and return an object.

        :param token: token value originating from the Authentication service
            either directly or from an upstream service
        :rtype: :py:class:`AuthenticationToken`

        """
        if not token:
            return InvalidAuthenticationToken()

        secret = self.secrets.get_versioned("secret/authentication/public-key")
        for public_key in secret.all_versions:
            try:
                decoded = jwt.decode(token, public_key, algorithms="RS256")
                return ValidatedAuthenticationToken(decoded)
            except jwt.ExpiredSignatureError:
                pass
            except jwt.DecodeError:
                pass

        return InvalidAuthenticationToken()


class AuthenticationToken:
    """Information about the authenticated user.

    :py:class:`EdgeRequestContext` provides high-level helpers for extracting
    data from authentication tokens. Use those instead of direct access through
    this class.

    """

    @property
    def subject(self):
        """Return the raw `subject` that is authenticated."""
        raise NotImplementedError

    @property
    def user_roles(self):
        raise NotImplementedError

    @property
    def oauth_client_id(self):
        raise NotImplementedError

    @property
    def oauth_client_type(self):
        raise NotImplementedError


class ValidatedAuthenticationToken(AuthenticationToken):
    def __init__(self, payload):
        self.payload = payload

    @property
    def subject(self):
        return self.payload.get("sub")

    @cached_property
    def user_roles(self):
        return set(self.payload.get("roles", []))

    @property
    def oauth_client_id(self):
        return self.payload.get("client_id")

    @property
    def oauth_client_type(self):
        return self.payload.get("client_type")


class InvalidAuthenticationToken(AuthenticationToken):
    @property
    def subject(self):
        raise NoAuthenticationError

    @property
    def user_roles(self):
        raise NoAuthenticationError

    @property
    def oauth_client_id(self):
        raise NoAuthenticationError

    @property
    def oauth_client_type(self):
        raise NoAuthenticationError


_User = collections.namedtuple("_User", ["authentication_token", "loid", "cookie_created_ms"])
_OAuthClient = collections.namedtuple("_OAuthClient", ["authentication_token"])
Session = collections.namedtuple("Session", ["id"])
_Service = collections.namedtuple("_Service", ["authentication_token"])


class User(_User):
    """Wrapper for the user values in AuthenticationToken and the LoId cookie."""

    @property
    def id(self):
        """Return the authenticated account_id for the current User.

        :type: account_id string or None if context authentication is invalid
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token, it was invalid, or the subject is not an
            account.

        """
        subject = self.authentication_token.subject
        if not (subject and subject.startswith("t2_")):
            raise NoAuthenticationError
        return subject

    @property
    def is_logged_in(self):
        """Return if the User has a valid, authenticated id."""
        try:
            return self.id is not None
        except NoAuthenticationError:
            return False

    @property
    def roles(self):
        """Return the authenticated roles for the current User.

        :type: set(string)
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token or it was invalid

        """
        return self.authentication_token.user_roles

    def has_role(self, role):
        """Return if the authenticated user has the specified role.

        :param str client_types: Case-insensitive sequence role name to check.

        :type: bool
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token defined for the current context

        """
        return role.lower() in self.roles

    def event_fields(self):
        """Return fields to be added to events."""
        if self.is_logged_in:
            user_id = self.id
        else:
            user_id = self.loid

        return {
            "user_id": user_id,
            "logged_in": self.is_logged_in,
            "cookie_created_timestamp": self.cookie_created_ms,
        }


class OAuthClient(_OAuthClient):
    """Wrapper for the OAuth2 client values in AuthenticationToken."""

    @property
    def id(self):
        """Return the authenticated id for the current client.

        :type: string or None if context authentication is invalid
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token defined for the current context

        """
        return self.authentication_token.oauth_client_id

    def is_type(self, *client_types):
        """Return if the authenticated client type is one of the given types.

        When checking the type of the current OauthClient, you should check
        that the type "is" one of the allowed types rather than checking that
        it "is not" a disallowed type.

        For example::

            if oauth_client.is_type("third_party"):
                ...

        not::

            if not oauth_client.is_type("first_party"):
                ...


        :param str client_types: Case-insensitive sequence of client type
            names that you want to check.

        :type: bool
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token defined for the current context

        """
        lower_types = (client_type.lower() for client_type in client_types)
        return self.authentication_token.oauth_client_type in lower_types

    def event_fields(self):
        """Return fields to be added to events."""
        try:
            oauth_client_id = self.id
        except NoAuthenticationError:
            oauth_client_id = None

        return {"oauth_client_id": oauth_client_id}


class Service(_Service):
    """Wrapper for the Service values in AuthenticationToken."""

    @property
    def name(self):
        """Return the authenticated service name.

        :type: name string or None if context authentication is invalid
        :raises: :py:class:`NoAuthenticationError` if there was no
            authentication token, it was invalid, or the subject is not a
            servce.

        """
        subject = self.authentication_token.subject
        if not (subject and subject.startswith("service/")):
            raise NoAuthenticationError

        name = subject[len("service/") :]
        return name


class EdgeRequestContextFactory:
    """Factory for creating :py:class:`EdgeRequestContext` objects.

    Every application should set one of these up. Edge services that talk
    directly with clients should use :py:meth:`new` directly. For internal
    services, pass the object off to Baseplate's framework integration
    (Thrift/Pyramid) for automatic use.

    :param baseplate.lib.secrets.SecretsStore secrets: A configured secrets
        store.

    """

    def __init__(self, secrets):
        self.authn_token_validator = AuthenticationTokenValidator(secrets)

    def new(self, authentication_token=None, loid_id=None, loid_created_ms=None, session_id=None):
        """Return a new EdgeRequestContext object made from scratch.

        Services at the edge that communicate directly with clients should use
        this to pass on the information they get to downstream services. They
        can then use this information to check authentication, run experiments,
        etc.

        To use this, create and attach the context early in your request flow:

        .. code-block:: python

            auth_cookie = request.cookies["authentication"]
            token = request.authentication_service.authenticate_cookie(cookie)
            loid = parse_loid(request.cookies["loid"])
            session = parse_session(request.cookies["session"])

            edge_context = self.edgecontext_factory.new(
                authentication_token=token,
                loid_id=loid.id,
                loid_created_ms=loid.created,
                session_id=session.id,
            )
            edge_context.attach_context(request)

        :param authentication_token: (Optional) A raw authentication token
            as returned by the authentication service.
        :param str loid_id: (Optional) ID for the current LoID in fullname
            format.
        :param int loid_created_ms: (Optional) Epoch milliseconds when the
            current LoID cookie was created.
        :param str session_id: (Optional) ID for the current session cookie.

        """
        # Importing the Thrift models inline so that building them is not a
        # hard, import-time dependency for tasks like building the docs.
        from baseplate.thrift.ttypes import Loid as TLoid
        from baseplate.thrift.ttypes import Request as TRequest
        from baseplate.thrift.ttypes import Session as TSession

        if loid_id is not None and not loid_id.startswith("t2_"):
            raise ValueError(
                "loid_id <%s> is not in a valid format, it should be in the "
                "fullname format with the '0' padding removed: 't2_loid_id'" % loid_id
            )

        t_request = TRequest(
            loid=TLoid(id=loid_id, created_ms=loid_created_ms),
            session=TSession(id=session_id),
            authentication_token=authentication_token,
        )
        header = TSerialization.serialize(t_request, EdgeRequestContext._HEADER_PROTOCOL_FACTORY)

        context = EdgeRequestContext(self.authn_token_validator, header)
        # Set the _t_request property so we can skip the deserialization step
        # since we already have the thrift object.
        context._t_request = t_request
        return context

    def from_upstream(self, edge_header):
        """Create and return an EdgeRequestContext from an upstream header.

        This is generally used internally to Baseplate by framework
        integrations that automatically pick up context from inbound requests.

        :param edge_header: Raw payload of Edge-Request header from upstream
            service.

        """
        return EdgeRequestContext(self.authn_token_validator, edge_header)


class EdgeRequestContext:
    """Contextual information about the initial request to an edge service.

    Construct this using an
    :py:class:`~baseplate.lib.edge_context.EdgeRequestContextFactory`.

    """

    _HEADER_PROTOCOL_FACTORY = TBinaryProtocolAcceleratedFactory()

    def __init__(self, authn_token_validator, header):
        self._authn_token_validator = authn_token_validator
        self._header = header

    def attach_context(self, context):
        """Attach this to the provided :term:`context object`.

        :param context: request context to attach this to

        """
        context.request_context = self
        context.raw_request_context = self._header

    def event_fields(self):
        """Return fields to be added to events."""
        fields = {"session_id": self.session.id}
        fields.update(self.user.event_fields())
        fields.update(self.oauth_client.event_fields())
        return fields

    @cached_property
    def authentication_token(self):
        return self._authn_token_validator.validate(self._t_request.authentication_token)

    @cached_property
    def user(self):
        """:py:class:`~baseplate.lib.edge_context.User` object for the current context."""
        return User(
            authentication_token=self.authentication_token,
            loid=self._t_request.loid.id,
            cookie_created_ms=self._t_request.loid.created_ms,
        )

    @cached_property
    def oauth_client(self):
        """:py:class:`~baseplate.lib.edge_context.OAuthClient` object for the current context."""
        return OAuthClient(self.authentication_token)

    @cached_property
    def session(self):
        """:py:class:`~baseplate.lib.edge_context.Session` object for the current context."""
        return Session(id=self._t_request.session.id)

    @cached_property
    def service(self):
        """:py:class:`~baseplate.lib.edge_context.Service` object for the current context."""
        return Service(self.authentication_token)

    @cached_property
    def _t_request(self):  # pylint: disable=method-hidden
        # Importing the Thrift models inline so that building them is not a
        # hard, import-time dependency for tasks like building the docs.
        from baseplate.thrift.ttypes import Loid as TLoid
        from baseplate.thrift.ttypes import Request as TRequest
        from baseplate.thrift.ttypes import Session as TSession

        _t_request = TRequest()
        _t_request.loid = TLoid()
        _t_request.session = TSession()
        if self._header:
            try:
                TSerialization.deserialize(_t_request, self._header, self._HEADER_PROTOCOL_FACTORY)
            except Exception:
                logger.debug("Invalid Edge-Request header. %s", self._header)
        return _t_request