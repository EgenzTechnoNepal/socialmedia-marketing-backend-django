import hashlib
import uuid

from django.db import models


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Organization(UUIDModel):
    name = models.CharField(max_length=255)
    slug = models.CharField(max_length=100, unique=True)
    settings = models.JSONField(default=dict)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "organizations"
        managed = False

    def __str__(self):
        return self.name


class User(UUIDModel):
    """Go users table. Not Django's contrib auth user."""

    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_column="organization_id", db_constraint=False
    )
    email = models.CharField(max_length=255, unique=True)
    password_hash = models.CharField(max_length=255)
    full_name = models.CharField(max_length=255, blank=True)
    role_id = models.UUIDField(null=True, blank=True)
    settings = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    is_available = models.BooleanField(default=True)
    is_super_admin = models.BooleanField(default=False)
    sso_provider = models.CharField(max_length=50, blank=True)
    sso_provider_id = models.CharField(max_length=255, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "users"
        managed = False

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def pk(self):
        return self.id

    def __str__(self):
        return self.email

    def has_permission(self, resource: str, action: str) -> bool:
        if self.is_super_admin or getattr(self, "is_super_admin_claim", False):
            return True
        if not self.role_id:
            return False
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                WHERE rp.custom_role_id = %s
                  AND p.resource = %s
                  AND p.action = %s
                  AND p.deleted_at IS NULL
                LIMIT 1
                """,
                [str(self.role_id), resource, action],
            )
            return cursor.fetchone() is not None


class CustomRole(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    is_system = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "custom_roles"
        managed = False

    def __str__(self):
        return self.name


class Permission(UUIDModel):
    resource = models.CharField(max_length=50)
    action = models.CharField(max_length=20)
    description = models.CharField(max_length=200, blank=True)

    objects = ActiveManager()

    class Meta:
        db_table = "permissions"
        managed = False


class RolePermission(models.Model):
    custom_role_id = models.UUIDField(primary_key=True)
    permission = models.ForeignKey(
        Permission, on_delete=models.DO_NOTHING, db_column="permission_id", db_constraint=False
    )

    class Meta:
        db_table = "role_permissions"
        managed = False
        unique_together = ("custom_role_id", "permission")


class UserOrganization(UUIDModel):
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    role_id = models.UUIDField(null=True, blank=True)
    is_default = models.BooleanField(default=False)

    objects = ActiveManager()

    class Meta:
        db_table = "user_organizations"
        managed = False


class SSOProvider(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    provider = models.CharField(max_length=50)
    client_id = models.CharField(max_length=500)
    client_secret = models.CharField(max_length=500)
    is_enabled = models.BooleanField(default=False)
    allow_auto_create = models.BooleanField(default=False)
    default_role_name = models.CharField(max_length=50, default="agent")
    allowed_domains = models.TextField(blank=True)
    auth_url = models.CharField(max_length=500, blank=True)
    token_url = models.CharField(max_length=500, blank=True)
    user_info_url = models.CharField(max_length=500, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "sso_providers"
        managed = False


class UserAvailabilityLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    is_available = models.BooleanField()
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_availability_logs"
        managed = False


class APIKey(UUIDModel):
    organization = models.ForeignKey(Organization, on_delete=models.DO_NOTHING, db_constraint=False)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    name = models.CharField(max_length=255)
    key_prefix = models.CharField(max_length=16)
    key_hash = models.CharField(max_length=255)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    objects = ActiveManager()

    class Meta:
        db_table = "api_keys"
        managed = False

    @classmethod
    def match(cls, raw: str):
        prefixes = []
        if raw.startswith("whm_") and len(raw) >= 12:
            prefixes.append(raw[4:20] if len(raw) >= 20 else raw[4:12])
            prefixes.append(raw[4:12])
        else:
            prefixes.append(raw[:16])
        digest = hashlib.sha256(raw.encode()).hexdigest()
        seen = set()
        for prefix in prefixes:
            if not prefix or prefix in seen:
                continue
            seen.add(prefix)
            for key in cls.objects.filter(key_prefix=prefix, is_active=True):
                stored = key.key_hash or ""
                if stored == digest:
                    return key
                try:
                    import bcrypt

                    if bcrypt.checkpw(raw.encode(), stored.encode()):
                        return key
                except Exception:
                    pass
        return None


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    resource_type = models.CharField(max_length=50)
    resource_id = models.UUIDField()
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_constraint=False)
    user_name = models.CharField(max_length=255)
    action = models.CharField(max_length=20)
    changes = models.JSONField(default=list)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "audit_logs"
        managed = False

