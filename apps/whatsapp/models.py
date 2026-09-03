from django.conf import settings
from django.db import models

from apps.accounts.models import Organization, User, UUIDModel, ActiveManager
from services.crypto import decrypt, encrypt


class WhatsAppAccount(UUIDModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.DO_NOTHING, db_constraint=False
    )
    name = models.CharField(max_length=100)
    app_id = models.CharField(max_length=100, blank=True)
    phone_id = models.CharField(max_length=100)
    business_id = models.CharField(max_length=100)
    access_token = models.TextField()
    app_secret = models.CharField(max_length=255, blank=True)
    webhook_verify_token = models.CharField(max_length=255, blank=True)
    api_version = models.CharField(max_length=20, default="v21.0")
    is_default_incoming = models.BooleanField(default=False)
    is_default_outgoing = models.BooleanField(default=False)
    auto_read_receipt = models.BooleanField(default=False)
    business_calling_enabled = models.BooleanField(default=False)
    is_smb = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default="active")
    pin = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        null=True,
        blank=True,
        db_constraint=False,
        related_name="+",
    )

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "whatsapp_accounts"
        managed = False

    def decrypt_secrets(self):
        key = settings.ENCRYPTION_KEY
        self.access_token = decrypt(self.access_token or "", key)
        self.app_secret = decrypt(self.app_secret or "", key)
        self.pin = decrypt(self.pin or "", key)
        return self

    def encrypt_secrets(self):
        key = settings.ENCRYPTION_KEY
        self.access_token = encrypt(self.access_token or "", key)
        self.app_secret = encrypt(self.app_secret or "", key)
        self.pin = encrypt(self.pin or "", key)
        return self


def resolve_account(oid, name: str):
    from apps.common.exceptions import APIError

    account = WhatsAppAccount.objects.filter(organization_id=oid, name=name).first()
    if not account:
        raise APIError("WhatsApp account not found", status_code=404)
    return account.decrypt_secrets()
