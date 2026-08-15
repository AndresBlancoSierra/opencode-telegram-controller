"""Network commands: /ip /dns /network.

Handlers are thin and registered onto the bot's router by :func:`register`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from .common import check_permission, format_service_error


async def on_ip(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/ip"):
        return
    if ctx.network is None:
        await message.answer("❌ Network capability is not available.")
        return
    try:
        status = await ctx.network.status()
    except Exception as exc:
        await message.answer(format_service_error("Network", exc))
        return

    lines = [
        "🌐 PUBLIC IP",
        "",
        f"Public: {status.public_ip or 'unreachable'}",
        f"Local: {status.local_ip or '-'}",
        f"Interface: {status.interface or '-'}",
        f"Gateway: {status.gateway or '-'}",
        "",
        "VPN:",
    ]
    if ctx.vpn is not None:
        try:
            vpn_status = await ctx.vpn.status()
            lines.append(vpn_status.label)
        except Exception as exc:
            lines.append(format_service_error("VPN", exc))
    else:
        lines.append("not configured")
    await message.answer("\n".join(lines))


async def on_dns(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/dns"):
        return
    if ctx.network is None:
        await message.answer("❌ Network capability is not available.")
        return
    try:
        dns = await ctx.network.dns()
    except Exception as exc:
        await message.answer(format_service_error("DNS", exc))
        return
    lines = ["🔎 DNS", f"Backend: {dns.backend}", ""]
    lines.append("Resolvers:")
    lines.extend(f"• {server}" for server in dns.servers or ["(none found)"])
    if dns.search_domains:
        lines.append("")
        lines.append("Search domains:")
        lines.extend(f"• {domain}" for domain in dns.search_domains)
    if dns.notes:
        lines.append("")
        lines.extend(f"ℹ️ {note}" for note in dns.notes)
    await message.answer("\n".join(lines))


async def on_network(message: Message, ctx) -> None:
    if not await check_permission(ctx, message, "/network"):
        return
    if ctx.network is None:
        await message.answer("❌ Network capability is not available.")
        return
    try:
        info = await ctx.network.network_info()
        gateway = await ctx.network.gateway()
    except Exception as exc:
        await message.answer(format_service_error("Network", exc))
        return
    lines = ["📡 NETWORK"]
    if not info.interfaces:
        lines.append("No interfaces detected.")
    for interface in info.interfaces:
        lines.append(
            f"• {interface.name}  [{interface.state}]"
            f"{'  ' + interface.ipv4 if interface.ipv4 else ''}"
        )
    if gateway:
        lines.append("")
        lines.append(f"Gateway: {gateway}")
    await message.answer("\n".join(lines))


def register(router: Router) -> None:
    """Register this command group onto an existing router."""
    router.message.register(on_ip, Command("ip"))
    router.message.register(on_dns, Command("dns"))
    router.message.register(on_network, Command("network"))
