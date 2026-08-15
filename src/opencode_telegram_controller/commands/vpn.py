"""VPN commands: /vpn /vpn_status /vpn_dedicated /vpn_change.

Handlers are thin and registered onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..services.vpn import VpnStatus
from .common import audit, check_permission, format_service_error


async def on_vpn(message: Message, ctx) -> None:
    """Show VPN status/menu, or connect to an allowlisted country."""
    if not await check_permission(ctx, message, "vpn"):
        return
    vpn = getattr(ctx, "vpn", None)
    if vpn is None:
        await message.answer("❌ VPN is disabled (OTC_VPN_PROVIDER=none).")
        return

    parts = (message.text or "").split()
    if len(parts) > 1:
        country = " ".join(parts[1:]).strip()
        try:
            target = vpn.resolve_target(country)
        except ValueError as exc:
            await message.answer(format_service_error("VPN", exc))
            return
        try:
            result = await vpn.connect(target)
        except Exception as exc:
            await message.answer(format_service_error("VPN", exc))
            return
        await audit(ctx, message, "vpn.connect", target=target.country)
        await message.answer(_render_vpn(result, vpn=vpn), parse_mode="HTML")
        return

    try:
        result = await vpn.status()
    except Exception as exc:
        await message.answer(format_service_error("VPN", exc))
        return
    settings_vpn = getattr(vpn, "settings", None)
    country_tokens = (
        sorted({c.lower() for c in settings_vpn.vpn_countries if c})
        if settings_vpn is not None
        else []
    )
    lines = [_render_vpn(result, vpn=vpn)]
    if not result.connected:
        lines.append("\n<i>Connect with:</i>")
        for token in country_tokens:
            lines.append(f"  · <code>/vpn {token}</code>")
        if getattr(ctx, "settings", None) and ctx.settings.vpn_dedicated_server:
            lines.append(f"  · <code>/vpn_dedicated</code> — {ctx.settings.vpn_dedicated_server}")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def on_vpn_status(message: Message, ctx) -> None:
    """Detailed VPN status including the public IP."""
    if not await check_permission(ctx, message, "vpn_status"):
        return
    vpn = getattr(ctx, "vpn", None)
    if vpn is None:
        await message.answer("❌ VPN is disabled (OTC_VPN_PROVIDER=none).")
        return
    try:
        result = await vpn.status()
    except Exception as exc:
        await message.answer(format_service_error("VPN", exc))
        return
    lines = [_render_vpn(result, vpn=vpn)]
    if result.connected:
        try:
            public = await ctx.network.public_ip()
            lines.append(f"Ip pública (sin VPN): <code>{public}</code>")
        except Exception:
            pass
    await message.answer("\n".join(lines), parse_mode="HTML")


async def on_vpn_dedicated(message: Message, ctx) -> None:
    """Connect to the dedicated VPN server."""
    if not await check_permission(ctx, message, "vpn_dedicated"):
        return
    vpn = getattr(ctx, "vpn", None)
    if vpn is None:
        await message.answer("❌ VPN is disabled (OTC_VPN_PROVIDER=none).")
        return
    server: str = ""
    if getattr(ctx, "settings", None):
        server = ctx.settings.vpn_dedicated_server or ""
    try:
        vpn.validate_server(server)
        result = await vpn.connect_dedicated(server)
    except Exception as exc:
        await message.answer(format_service_error("VPN", exc))
        return
    await audit(ctx, message, "vpn.connect_dedicated", target=server)
    await message.answer(_render_vpn(result, vpn=vpn), parse_mode="HTML")


async def on_vpn_change(message: Message, ctx) -> None:
    """Reconnect the VPN (disconnect + connect), like the bash alias ``cambiar``."""
    if not await check_permission(ctx, message, "vpn_change"):
        return
    vpn = getattr(ctx, "vpn", None)
    if vpn is None:
        await message.answer("❌ VPN is disabled (OTC_VPN_PROVIDER=none).")
        return
    try:
        result = await vpn.reconnect()
    except Exception as exc:
        await message.answer(format_service_error("VPN", exc))
        return
    await audit(ctx, message, "vpn.reconnect")
    await message.answer(_render_vpn(result, vpn=vpn), parse_mode="HTML")


def _render_vpn(result: VpnStatus, *, vpn) -> str:
    lines = ["<b>🔐 VPN</b>"]
    lines.append(f"Estado: <b>{result.label}</b>")
    if result.provider:
        lines.append(f"Proveedor: <code>{result.provider}</code>")
    if result.server:
        lines.append(f"Servidor: <code>{result.server}</code>")
    if result.country:
        lines.append(f"País: <code>{result.country}</code>")
    if result.ip:
        lines.append(f"IP del túnel: <code>{result.ip}</code>")
    if result.kill_switch is not None:
        switch = "enabled" if result.kill_switch else "disabled"
        lines.append(f"Kill switch: <code>{switch}</code>")
    return "\n".join(lines)


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_vpn, Command("vpn"))
    router.message.register(on_vpn_status, Command("vpn_status"))
    router.message.register(on_vpn_dedicated, Command("vpn_dedicated"))
    router.message.register(on_vpn_change, Command("vpn_change"))
    router.message.register(on_vpn_change, Command("cambiar"))
