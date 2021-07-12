import enum
import re
from collections import namedtuple

import slugify
import sqlalchemy as sa
from pyramid import security

from h import pubid
from h.auth import role
from h.db import Base, mixins
from h.util.group import split_groupid

GROUP_NAME_MIN_LENGTH = 3
GROUP_NAME_MAX_LENGTH = 25
GROUP_DESCRIPTION_MAX_LENGTH = 250
AUTHORITY_PROVIDED_ID_PATTERN = r"^[a-zA-Z0-9._\-+!~*()']+$"
AUTHORITY_PROVIDED_ID_MAX_LENGTH = 1024


class JoinableBy(enum.Enum):
    authority = "authority"


class ReadableBy(enum.Enum):
    members = "members"
    world = "world"


class WriteableBy(enum.Enum):
    authority = "authority"
    members = "members"


class GroupMembership(Base):
    __tablename__ = "user_group"

    __table_args__ = (sa.UniqueConstraint("user_id", "group_id"),)

    id = sa.Column("id", sa.Integer, autoincrement=True, primary_key=True)
    user_id = sa.Column("user_id", sa.Integer, sa.ForeignKey("user.id"), nullable=False)
    group_id = sa.Column(
        "group_id", sa.Integer, sa.ForeignKey("group.id"), nullable=False
    )


class Group(Base, mixins.Timestamps):
    __tablename__ = "group"

    __table_args__ = (
        # Add a composite index of the (authority, authority_provided_id)
        # columns. Also impose uniqueness such that no two records may share
        # the same (authority, authority_provided_id) composite
        #
        # See:
        #
        # * http://docs.sqlalchemy.org/en/latest/core/constraints.html#indexes
        sa.Index(
            "ix__group__groupid", "authority", "authority_provided_id", unique=True
        ),
    )

    id = sa.Column(sa.Integer, autoincrement=True, primary_key=True)
    # We don't expose the integer PK to the world, so we generate a short
    # random string to use as the publicly visible ID.
    pubid = sa.Column(sa.Text(), default=pubid.generate, unique=True, nullable=False)
    authority = sa.Column(sa.UnicodeText(), nullable=False)
    name = sa.Column(sa.UnicodeText(), nullable=False, index=True)

    creator_id = sa.Column(sa.Integer, sa.ForeignKey("user.id"))
    creator = sa.orm.relationship("User")

    description = sa.Column(sa.UnicodeText())

    #: Enforce scope match for annotations in this group.
    #: For groups with 1-n scopes, only allow annotations for target
    #: documents whose URIs match one of the group's scopes.
    #: When disabled, annotations should be allowed web-wide.
    #: This setting has no effect if the group does not have any scopes.
    #: Enforcement is the responsibility of services (i.e. the model
    #: layer does not enforce scope compliance).
    enforce_scope = sa.Column(
        sa.Boolean,
        nullable=False,
        default=True,
        server_default=sa.sql.expression.true(),
    )

    #: Allow authorities to define their own unique identifier for a group
    #: (versus the pubid). This identifier is owned by the authority/client
    #: versus ``pubid``, which is owned and controlled by the service.
    authority_provided_id = sa.Column(sa.UnicodeText(), nullable=True)

    #: Which type of user is allowed to join this group, possible values are:
    #: authority, None
    joinable_by = sa.Column(
        sa.Enum(JoinableBy, name="group_joinable_by"), nullable=True
    )

    #: Which type of user is allowed to read annotations in this group,
    #: possible values are: authority, members, world
    readable_by = sa.Column(
        sa.Enum(ReadableBy, name="group_readable_by"), nullable=True, index=True
    )

    #: Which type of user is allowed to write to this group, possible values
    #: are: authority, members
    writeable_by = sa.Column(
        sa.Enum(WriteableBy, name="group_writeable_by"), nullable=True
    )

    @property
    def groupid(self):
        if self.authority_provided_id is None:
            return None
        return "group:{authority_provided_id}@{authority}".format(
            authority_provided_id=self.authority_provided_id, authority=self.authority
        )

    @groupid.setter
    def groupid(self, value):
        """
        Deconstruct a formatted ``groupid`` and set its constituent properties
        on the instance.

        If ``groupid`` is set to None, set ``authority_provided_id`` to None
        but leave authority untouched—this allows a caller to nullify the
        ``authority_provided_id`` field.

        :raises ValueError: if ``groupid`` is an invalid format
        """
        if value is None:
            self.authority_provided_id = None
        else:
            groupid_parts = split_groupid(value)
            self.authority_provided_id = groupid_parts["authority_provided_id"]
            self.authority = groupid_parts["authority"]

    # Group membership
    members = sa.orm.relationship(
        "User",
        secondary="user_group",
        backref=sa.orm.backref("groups", order_by="Group.name"),
    )

    scopes = sa.orm.relationship(
        "GroupScope", backref="group", cascade="all, delete-orphan"
    )

    organization_id = sa.Column(
        sa.Integer, sa.ForeignKey("organization.id"), nullable=True
    )
    organization = sa.orm.relationship("Organization")

    def __init__(self, **kwargs):
        super(Group, self).__init__(**kwargs)

    @sa.orm.validates("name")
    def validate_name(self, _key, name):
        if not GROUP_NAME_MIN_LENGTH <= len(name) <= GROUP_NAME_MAX_LENGTH:
            raise ValueError(
                "name must be between {min} and {max} characters "
                "long".format(min=GROUP_NAME_MIN_LENGTH, max=GROUP_NAME_MAX_LENGTH)
            )
        return name

    @sa.orm.validates("authority_provided_id")
    def validate_authority_provided_id(self, _key, authority_provided_id):
        if not authority_provided_id:
            return None

        if not re.match(AUTHORITY_PROVIDED_ID_PATTERN, authority_provided_id):
            raise ValueError(
                "authority_provided_id must only contain characters allowed"
                r" in encoded URIs: [a-zA-Z0-9._\-+!~*()']"
            )

        if len(authority_provided_id) > AUTHORITY_PROVIDED_ID_MAX_LENGTH:
            raise ValueError(
                "authority_provided_id must be {max} characters or fewer"
                "characters long".format(max=AUTHORITY_PROVIDED_ID_MAX_LENGTH)
            )

        return authority_provided_id

    @property
    def slug(self):
        """A version of this group's name suitable for use in a URL."""
        return slugify.slugify(self.name)

    @property
    def type(self):
        """
        The "type" of this group, e.g. "open" or "private".

        :rtype: string
        :raises ValueError: if the type of the group isn't recognized

        """
        self_type_flags = TypeFlags(
            joinable_by=self.joinable_by,
            readable_by=self.readable_by,
            writeable_by=self.writeable_by,
        )

        for type_, type_flags in (
            ("open", OPEN_GROUP_TYPE_FLAGS),
            ("private", PRIVATE_GROUP_TYPE_FLAGS),
            ("restricted", RESTRICTED_GROUP_TYPE_FLAGS),
        ):
            if self_type_flags == type_flags:
                return type_

        raise ValueError(
            "This group doesn't seem to match any known type of group. "
            "This shouldn't be in the database!"
        )

    @property
    def is_public(self):
        return self.readable_by == ReadableBy.world

    def __acl__(self):
        terms = []

        join_principal = _join_principal(self)
        if join_principal is not None:
            terms.append((security.Allow, join_principal, "join"))

        read_principal = _read_principal(self)
        if read_principal is not None:
            terms.append((security.Allow, read_principal, "read"))
            # Any user who can read the group should also be able to see
            # who is a member of the group
            terms.append((security.Allow, read_principal, "member_read"))

        flag_principal = _flag_principal(self)
        if flag_principal is not None:
            terms.append((security.Allow, flag_principal, "flag"))

        write_principal = _write_principal(self)
        if write_principal is not None:
            terms.append((security.Allow, write_principal, "write"))

        if self.creator:
            # The creator of the group should be able to update it
            terms.append((security.Allow, self.creator.userid, "admin"))
            terms.append((security.Allow, self.creator.userid, "moderate"))
            # The creator may update this group in an upsert context
            terms.append((security.Allow, self.creator.userid, "upsert"))

        # Temporary hack to allow the LMS app's machine user to upsert all LMS
        # groups, even if the machine user isn't the group's creator.
        # We can remove this once we've either:
        #
        # * Run a DB migration to change the creators of all LMS groups to the
        #   LMS app's machine user: https://github.com/hypothesis/lms/issues/1401
        # * Or changed the LMS app to use h's new bulk API instead of using the
        #   group upsert API: https://github.com/hypothesis/lms/issues/1506
        if self.authority == "lms.hypothes.is":
            terms.append((security.Allow, "acct:lms@lms.hypothes.is", "upsert"))

        # This authority principal may be used to grant auth clients
        # permissions for groups within their authority
        authority_principal = "client_authority:{}".format(self.authority)

        # auth_clients that have the same authority as the target group
        # may read the members within it
        terms.append((security.Allow, authority_principal, "member_read"))
        # auth_clients that have the same authority as the target group
        # may add members to it
        terms.append((security.Allow, authority_principal, "member_add"))
        # auth_clients that have the same authority as this group
        # should be allowed to update it
        terms.append((security.Allow, authority_principal, "admin"))
        # auth_clients with matching authority should be able to read
        # the group
        terms.append((security.Allow, authority_principal, "read"))

        # Those with the admin or staff role should be able to admin/edit any group
        terms.append((security.Allow, role.Staff, "admin"))
        terms.append((security.Allow, role.Admin, "admin"))

        terms.append(security.DENY_ALL)

        return terms

    def __repr__(self):
        return "<Group: %s>" % self.slug

    @classmethod
    def created_by(cls, session, user):
        """Return a query object filtering groups by creator."""
        return session.query(cls).filter(Group.creator == user)


def _join_principal(group):
    return {JoinableBy.authority: "authority:{}".format(group.authority)}.get(
        group.joinable_by
    )


def _read_principal(group):
    return {
        ReadableBy.members: "group:{}".format(group.pubid),
        ReadableBy.world: security.Everyone,
    }.get(group.readable_by)


def _flag_principal(group):
    # If a user can read (see) annotations within this group,
    # they can also flag them—but they need to be logged in
    # (``pyramid.security.Authenticated``)
    return {
        ReadableBy.members: "group:{}".format(group.pubid),
        ReadableBy.world: security.Authenticated,
    }.get(group.readable_by)


def _write_principal(group):
    return {
        WriteableBy.authority: "authority:{}".format(group.authority),
        WriteableBy.members: "group:{}".format(group.pubid),
    }.get(group.writeable_by)


TypeFlags = namedtuple("TypeFlags", "joinable_by readable_by writeable_by")


OPEN_GROUP_TYPE_FLAGS = TypeFlags(
    joinable_by=None, readable_by=ReadableBy.world, writeable_by=WriteableBy.authority
)


PRIVATE_GROUP_TYPE_FLAGS = TypeFlags(
    joinable_by=JoinableBy.authority,
    readable_by=ReadableBy.members,
    writeable_by=WriteableBy.members,
)


RESTRICTED_GROUP_TYPE_FLAGS = TypeFlags(
    joinable_by=None, readable_by=ReadableBy.world, writeable_by=WriteableBy.members
)
