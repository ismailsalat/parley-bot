"""Owner-authored, per-server partner posts for #find-partners."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.database import repository
from bot.database.models import Listing, ListingStatus
from bot.services import partnerships, permissions
from bot.services.errors import LISTING_GONE, NotFound, ValidationError
from bot.utils.helpers import listing_jump_url, truncate, utcnow
from bot.utils.mentions import safe_allowed_mentions
from bot.views import self_post
from bot.views.base import ConfirmView, OwnedView, acknowledge, get_bot, handle_error, home_button, reply
from bot.views.welcome import action_button, add_bot_button, persistent_view, register_action, show_screen

if TYPE_CHECKING:
    from bot.core import ParleyBot

log = logging.getLogger(__name__)


def partner_post_url(bot: ParleyBot, listing: Listing) -> str | None:
    return listing_jump_url(
        bot.runtime.hub.main_guild_id,
        listing.partner_channel_id,
        listing.partner_message_id,
    )


def partner_post_controls(bot: ParleyBot, guild_id: int) -> discord.ui.View:
    """Actions under a partner post are intentionally different from server-directory actions."""
    from bot.views.partnership import RequestPartnershipButton, view_ad_button

    return persistent_view(
        view_ad_button(bot, guild_id, label="View Server"),
        RequestPartnershipButton(
            guild_id,
            label="Request Partnership",
            emoji="🤝",
            style=discord.ButtonStyle.success,
        ),
    )


async def _active_managed_listings(bot: ParleyBot, user_id: int) -> tuple[list[Listing], dict[int, discord.Guild]]:
    guilds = {g.id: g for g in permissions.cached_manageable_guilds(bot, user_id)}
    async with bot.db.session() as session:
        rows = await repository.get_listings(session, guilds.keys())
    rows = [
        row for row in rows
        if row.status == ListingStatus.ACTIVE and row.guild_id in guilds and permissions.is_connected(bot, row.guild_id)
    ]
    rows.sort(key=lambda row: guilds[row.guild_id].name.lower())
    return rows, guilds


@register_action("partner_posts")
async def partner_posts_action(interaction: discord.Interaction) -> None:
    await start_partner_posts(interaction)


async def start_partner_posts(interaction: discord.Interaction) -> None:
    """Open the one-per-server partner-post manager, using a dropdown only when needed."""
    bot = get_bot(interaction)
    rows, guilds = await _active_managed_listings(bot, interaction.user.id)

    if not rows:
        async with bot.db.session() as session:
            verified_ids = set(await repository.guild_ids_connected_by(session, interaction.user.id))
            listed = [
                row for row in await repository.get_listings(session, verified_ids)
                if row.status == ListingStatus.ACTIVE
            ]
        if listed:
            embed = discord.Embed(
                title="✨ Connect Parley for Partner Posts",
                description=(
                    "Your directory listings are still live. Partner Board posts are a **Parley Connected** perk, "
                    "so add Parley to the server you want to post for."
                ),
                color=bot.runtime.bot.color_primary,
            )
            await show_screen(
                interaction,
                embed=embed,
                view=persistent_view(add_bot_button(bot), action_button(bot, "servers"), home_button(bot)),
            )
        else:
            embed = discord.Embed(
                title="🤝 List a Server First",
                description="Partner Board posts belong to one of your server listings. List a server to get started.",
                color=bot.runtime.bot.color_primary,
            )
            await show_screen(
                interaction,
                embed=embed,
                view=persistent_view(action_button(bot, "post"), home_button(bot)),
            )
        return

    if len(rows) == 1:
        await show_partner_management(interaction, rows[0].guild_id)
        return

    from bot.views.partnership import GuildPickerView

    async def picked(inter: discord.Interaction, guild_id: int) -> None:
        await show_partner_management(inter, guild_id)

    embed = discord.Embed(
        title="🤝 My Partner Posts",
        description="Choose which server post you want to manage.",
        color=bot.runtime.bot.color_primary,
    )
    await show_screen(
        interaction,
        embed=embed,
        view=GuildPickerView(
            interaction.user.id,
            [(row.guild_id, guilds[row.guild_id].name) for row in rows],
            picked,
            placeholder="Select your server",
        ),
    )


class PartnerPostManager(OwnedView):
    def __init__(
        self,
        bot: ParleyBot,
        owner_id: int,
        guild: discord.Guild,
        listing: Listing,
    ) -> None:
        super().__init__(owner_id)
        self.bot = bot
        self.guild = guild
        self.listing = listing
        self._build()

    def _build(self) -> None:
        live = bool(self.listing.partner_message_id and self.listing.partner_channel_id)
        if live:
            edit = discord.ui.Button(label="Edit Post", style=discord.ButtonStyle.primary, row=0)
            edit.callback = self._edit  # type: ignore[method-assign]
            self.add_item(edit)

            url = partner_post_url(self.bot, self.listing)
            if url:
                self.add_item(discord.ui.Button(label="View Post", url=url, row=0))

            delete = discord.ui.Button(label="Delete Post", style=discord.ButtonStyle.danger, row=1)
            delete.callback = self._delete  # type: ignore[method-assign]
            self.add_item(delete)
        else:
            post = discord.ui.Button(label="Post to Partner Board", style=discord.ButtonStyle.success, row=0)
            post.callback = self._post  # type: ignore[method-assign]
            self.add_item(post)

        self.add_item(home_button(self.bot, row=2))

    async def _post(self, interaction: discord.Interaction) -> None:
        try:
            await open_partner_post_window(interaction, self.guild.id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)

    async def _edit(self, interaction: discord.Interaction) -> None:
        try:
            await open_partner_post_window(interaction, self.guild.id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)

    async def _delete(self, interaction: discord.Interaction) -> None:
        try:
            await delete_partner_post(interaction, self.guild.id)
        except Exception as exc:  # noqa: BLE001
            await handle_error(interaction, exc)


def partner_management_embed(bot: ParleyBot, guild: discord.Guild, listing: Listing) -> discord.Embed:
    live = bool(listing.partner_message_id and listing.partner_channel_id)
    embed = discord.Embed(
        title="🤝 Partner Board Post",
        description=f"**{guild.name}**\n{'🟢 Live on the Partner Board' if live else '⚪ No Partner Board post yet'}",
        color=bot.runtime.bot.color_primary,
    )
    if listing.partner_ad_text:
        embed.add_field(
            name="Current post",
            value=truncate(listing.partner_ad_text, 850),
            inline=False,
        )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="Each listed server has its own post. People can request a partnership directly from it.")
    return embed


async def _load(bot: ParleyBot, guild_id: int, user_id: int) -> tuple[discord.Guild, Listing]:
    guild, _member = await permissions.require_manager(bot, guild_id, user_id)
    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
    if listing is None or listing.status != ListingStatus.ACTIVE:
        raise NotFound(LISTING_GONE)
    return guild, listing


async def show_partner_management(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing = await _load(bot, guild_id, interaction.user.id)
    await show_screen(
        interaction,
        embed=partner_management_embed(bot, guild, listing),
        view=PartnerPostManager(bot, interaction.user.id, guild, listing),
    )


async def _adopt_partner_post(
    bot: ParleyBot,
    guild_id: int,
    text: str,
    message: discord.Message,
    actor_id: int,
) -> None:
    channel = bot.panels.looking_channel()
    if channel is None or message.channel.id != channel.id:
        raise ValidationError("The Partner Board channel changed while you were posting. Try again.")

    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
        if listing is None or listing.status != ListingStatus.ACTIVE:
            raise NotFound(LISTING_GONE)
        old_channel = listing.partner_channel_id
        old_message = listing.partner_message_id
        old_controls = listing.partner_controls_message_id

    controls = await channel.send(
        content="-# 🤝 Partner Board actions",
        view=partner_post_controls(bot, guild_id),
        allowed_mentions=safe_allowed_mentions(),
    )

    now = utcnow()
    try:
        async with bot.db.session() as session:
            # The configured cooldown is per server, so another admin cannot immediately
            # repost the same community and flood the Partner Board.
            await partnerships.claim_looking_post(
                session,
                bot.runtime,
                source_guild_id=guild_id,
                actor_id=actor_id,
                message=text,
                now=now,
            )
            current = await repository.get_listing(session, guild_id)
            if current is None:
                raise NotFound(LISTING_GONE)
            current.partner_ad_text = text
            current.partner_channel_id = channel.id
            current.partner_message_id = message.id
            current.partner_controls_message_id = controls.id
            current.partner_posted_at = now
            current.accepting_partnerships = True
            current.updated_at = now
            await repository.add_audit(
                session,
                "partner_post.saved",
                actor_id=actor_id,
                guild_id=guild_id,
                details={"message_id": message.id},
            )
    except Exception:
        try:
            await controls.delete()
        except discord.HTTPException:
            pass
        raise

    if old_message and old_message != message.id:
        await bot.panels.delete_listing_message(old_channel, old_message)
    if old_controls and old_controls != controls.id:
        await bot.panels.delete_listing_message(old_channel, old_controls)

    await bot.panels.move_looking_panel_now()
    log.info("partner_post.saved guild_id=%s message_id=%s", guild_id, message.id)


async def open_partner_post_window(interaction: discord.Interaction, guild_id: int) -> None:
    await acknowledge(interaction)
    bot = get_bot(interaction)
    guild, _listing = await _load(bot, guild_id, interaction.user.id)
    channel = bot.panels.looking_channel()
    if channel is None:
        raise ValidationError("The Partner Board channel isn't set up yet.")

    async def accepted(text: str, message: discord.Message) -> None:
        await _adopt_partner_post(bot, guild_id, text, message, interaction.user.id)

    note = await self_post.run_submission(
        interaction,
        guild_id,
        guild.name,
        channel=channel,
        post_title="Partner ad",
        retry_hint="My Partner Posts",
        allow_review=False,
        respect_approval_required=False,
        on_accept=accepted,
        success_text="## Posted to Partner Board ✅\nYour server is now visible to people actively looking for partners.",
    )
    await interaction.edit_original_response(
        content=note,
        view=persistent_view(action_button(bot, "partner_posts"), home_button(bot)),
    )


async def _clear_partner_fields(
    bot: ParleyBot,
    guild_id: int,
    *,
    actor_id: int | None = None,
) -> tuple[int | None, int | None, int | None]:
    async with bot.db.session() as session:
        listing = await repository.get_listing(session, guild_id)
        if listing is None:
            return None, None, None
        old = (listing.partner_channel_id, listing.partner_message_id, listing.partner_controls_message_id)
        listing.partner_ad_text = None
        listing.partner_channel_id = None
        listing.partner_message_id = None
        listing.partner_controls_message_id = None
        listing.partner_posted_at = None
        listing.updated_at = utcnow()
        if actor_id is not None:
            await repository.add_audit(session, "partner_post.deleted", actor_id=actor_id, guild_id=guild_id)
        return old


async def delete_partner_post(interaction: discord.Interaction, guild_id: int) -> None:
    bot = get_bot(interaction)
    guild, listing = await _load(bot, guild_id, interaction.user.id)
    if not listing.partner_message_id:
        await show_partner_management(interaction, guild_id)
        return

    confirm = ConfirmView(interaction.user.id, confirm_label="Delete Post")
    await reply(interaction, f"Delete the partner post for **{guild.name}**?", view=confirm)
    await confirm.wait()
    if not confirm.confirmed or confirm.interaction is None:
        return

    done = confirm.interaction
    await done.response.defer()
    await permissions.require_manager(bot, guild_id, done.user.id)
    channel_id, message_id, controls_id = await _clear_partner_fields(bot, guild_id, actor_id=done.user.id)
    await bot.panels.delete_listing_message(channel_id, message_id)
    await bot.panels.delete_listing_message(channel_id, controls_id)
    await bot.panels.move_looking_panel_now()
    await done.edit_original_response(content=f"Partner post deleted for **{guild.name}**.", view=None)


async def handle_direct_edit(bot: ParleyBot, before: discord.Message, after: discord.Message) -> None:
    """Direct Discord edits are removed so moderation cannot be bypassed."""
    async with bot.db.session() as session:
        listing = await repository.get_listing_by_partner_message(session, after.id)
    if listing is None:
        return

    controls_id = listing.partner_controls_message_id
    channel_id = listing.partner_channel_id
    guild_id = listing.guild_id
    try:
        await after.delete()
    except discord.HTTPException as exc:
        log.warning("Could not remove directly edited partner post %s: %s", after.id, exc)
        return

    await _clear_partner_fields(bot, guild_id)
    await bot.panels.delete_listing_message(channel_id, controls_id)
    await bot.panels.move_looking_panel_now()

    try:
        await after.author.send(
            "Your partner post was edited directly, so Parley removed it so changes cannot bypass moderation.\n\n"
            "Use **My Partner Posts → Edit Post** to post the replacement.",
            view=persistent_view(action_button(bot, "partner_posts")),
        )
    except discord.HTTPException:
        pass


async def handle_raw_delete(bot: ParleyBot, channel_id: int, message_id: int) -> None:
    """Clear stored state if an owner manually deletes their live partner post."""
    async with bot.db.session() as session:
        listing = await repository.get_listing_by_partner_message(session, message_id)
    if listing is None:
        return

    controls_id = listing.partner_controls_message_id
    guild_id = listing.guild_id
    await _clear_partner_fields(bot, guild_id)
    await bot.panels.delete_listing_message(channel_id, controls_id)
    await bot.panels.move_looking_panel_now()
