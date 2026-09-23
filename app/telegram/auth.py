"""Interactive login: phone + verification code (+ optional 2FA password), or bot token."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telethon import errors

from app.telegram.client import TelegramClientManager

log = logging.getLogger("app.telegram.auth")


class AuthError(Exception):
    """A user-facing authentication problem."""


class PasswordRequired(Exception):
    """The account has two-step verification enabled; a password is needed."""


@dataclass
class _PendingLogin:
    phone: str
    phone_code_hash: str


class AuthFlow:
    def __init__(self, manager: TelegramClientManager) -> None:
        self.manager = manager
        self._pending: _PendingLogin | None = None

    @property
    def _client(self):
        if self.manager.client is None:
            raise AuthError("Telegram client is not configured. Save your API ID and API hash first.")
        return self.manager.client

    async def request_code(self, phone: str) -> None:
        phone = phone.strip().replace(" ", "")
        if not phone:
            raise AuthError("Enter the phone number of your Telegram account in international format (+15551234567).")
        client = self._client
        if not client.is_connected():
            await self.manager.connect(max_attempts=3)
        try:
            sent = await client.send_code_request(phone)
        except errors.PhoneNumberInvalidError as exc:
            raise AuthError("That phone number is not valid. Use international format, e.g. +15551234567.") from exc
        except errors.PhoneNumberBannedError as exc:
            raise AuthError("This phone number is banned by Telegram.") from exc
        except errors.PhoneNumberFloodError as exc:
            raise AuthError("Too many login attempts. Telegram asks you to wait before trying again.") from exc
        except errors.ApiIdInvalidError as exc:
            raise AuthError("The API ID / API hash combination is invalid. Check my.telegram.org.") from exc
        except errors.FloodWaitError as exc:
            raise AuthError(f"Telegram rate limit: wait {exc.seconds} seconds before requesting a new code.") from exc
        self._pending = _PendingLogin(phone=phone, phone_code_hash=sent.phone_code_hash)
        log.info("Verification code sent to the Telegram app / SMS for the configured phone")

    async def submit_code(self, code: str) -> None:
        if self._pending is None:
            raise AuthError("Request a verification code first.")
        code = code.strip().replace(" ", "").replace("-", "")
        try:
            await self._client.sign_in(
                phone=self._pending.phone, code=code, phone_code_hash=self._pending.phone_code_hash
            )
        except errors.SessionPasswordNeededError as exc:
            raise PasswordRequired() from exc
        except errors.PhoneCodeInvalidError as exc:
            raise AuthError("The verification code is incorrect.") from exc
        except errors.PhoneCodeExpiredError as exc:
            self._pending = None
            raise AuthError("The verification code expired. Request a new one.") from exc
        except errors.PhoneNumberUnoccupiedError as exc:
            raise AuthError("No Telegram account exists for this phone number.") from exc
        except errors.FloodWaitError as exc:
            raise AuthError(f"Telegram rate limit: wait {exc.seconds} seconds and try again.") from exc
        self._pending = None

    async def submit_password(self, password: str) -> None:
        try:
            await self._client.sign_in(password=password)
        except errors.PasswordHashInvalidError as exc:
            raise AuthError("The two-step verification password is incorrect.") from exc
        except errors.FloodWaitError as exc:
            raise AuthError(f"Telegram rate limit: wait {exc.seconds} seconds and try again.") from exc
        self._pending = None

    async def login_bot(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise AuthError("Enter the bot token from @BotFather.")
        client = self._client
        if not client.is_connected():
            await self.manager.connect(max_attempts=3)
        try:
            await client.sign_in(bot_token=token)
        except errors.AccessTokenInvalidError as exc:
            raise AuthError("The bot token is invalid.") from exc
        except errors.FloodWaitError as exc:
            raise AuthError(f"Telegram rate limit: wait {exc.seconds} seconds and try again.") from exc

    async def logout(self) -> None:
        client = self.manager.client
        if client is not None and client.is_connected():
            try:
                await client.log_out()
            except errors.RPCError as exc:
                log.warning("Telegram log-out call failed (%s); removing the local session anyway", exc)
        await self.manager.disconnect()
        self.manager.delete_session()
        self.manager.me = None
        self._pending = None
