"""Heavily inspired by @mikeshardmind's one-file bots, which may explain if this looks familiar."""

from __future__ import annotations

import argparse
import asyncio
import functools
import getpass
import json
import logging
import os
from datetime import timedelta
from itertools import chain
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self, TypeAlias

import apsw
import apsw.bestpractice
import base2048
import discord
import platformdirs
import wavelink
import xxhash
from discord import app_commands
from discord.ext import tasks


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None

RadioInfoTuple: TypeAlias = tuple[int, int, str, int]

# Set up logging.
apsw.bestpractice.apply(apsw.bestpractice.recommended)  # type: ignore # SQLite WAL mode, logging, and other things.
discord.utils.setup_logging()
_log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-radiobot", "Sachaa-Thanasius", roaming=False)
escape_markdown = functools.partial(discord.utils.escape_markdown, as_needed=True)

MUSIC_EMOJIS: dict[str, str] = {
    "youtube": "<:youtube:1108460195270631537>",
    "youtubemusic": "<:youtubemusic:954046930713985074>",
    "soundcloud": "<:soundcloud:1147265178505846804>",
    "spotify": "<:spotify:1108458132826501140>",
}

INITIALIZATION_STATEMENTS = """
CREATE TABLE IF NOT EXISTS guild_radios (
    guild_id        INTEGER         NOT NULL        PRIMARY KEY,
    channel_id      INTEGER         NOT NULL,
    station_link    TEXT            NOT NULL,
    always_shuffle  INTEGER         NOT NULL        DEFAULT TRUE
) STRICT, WITHOUT ROWID;
"""

SELECT_ALL_BY_GUILD_STATEMENT = """
SELECT guild_id, channel_id, station_link, always_shuffle FROM guild_radios WHERE guild_id = ?;
"""

SELECT_ENABLED_GUILDS_STATEMENT = """
SELECT guild_id FROM guild_radios;
"""

UPSERT_GUILD_RADIO_STATEMENT = """
INSERT INTO guild_radios(guild_id, channel_id, station_link, always_shuffle)
VALUES (?, ?, ?, ?)
ON CONFLICT (guild_id)
DO UPDATE
    SET channel_id = EXCLUDED.channel_id,
        station_link = EXCLUDED.station_link,
        always_shuffle = EXCLUDED.always_shuffle
RETURNING *;
"""

DELETE_RADIO_BY_GUILD_STATEMENT = """
DELETE FROM guild_radios WHERE guild_id = ?;
"""


class LavalinkCreds(NamedTuple):
    uri: str
    password: str


class GuildRadioInfo(NamedTuple):
    guild_id: int
    channel_id: int
    station_link: str
    always_shuffle: bool

    @classmethod
    def from_row(cls: type[Self], row: RadioInfoTuple) -> Self:
        guild_id, channel_id, station_link, always_shuffle = row
        return cls(guild_id, channel_id, station_link, bool(always_shuffle))

    def display_embed(self) -> discord.Embed:
        """Format the radio's information into a Discord embed."""

        return (
            discord.Embed(title="Current Guild's Radio")
            .add_field(name="Channel", value=f"<#{self.channel_id}>")
            .add_field(name="Station", value=f"[Source]({self.station_link})")
            .add_field(name="Always Shuffle", value=("Yes" if self.always_shuffle else "No"))
        )


def _setup_db(conn: apsw.Connection) -> set[int]:
    with conn:
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENTS)
        cursor.execute(SELECT_ENABLED_GUILDS_STATEMENT)
        return set(chain.from_iterable(cursor))


def _delete(conn: apsw.Connection, query_str: str, params: apsw.Bindings | None = None) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(query_str, params)


def _query(conn: apsw.Connection, guild_ids: list[tuple[int]]) -> list[GuildRadioInfo]:
    cursor = conn.cursor()
    return [GuildRadioInfo.from_row(row) for row in cursor.executemany(SELECT_ALL_BY_GUILD_STATEMENT, guild_ids)]


def _add_radio(
    conn: apsw.Connection,
    *,
    guild_id: int,
    channel_id: int,
    station_link: str,
    always_shuffle: bool,
) -> GuildRadioInfo | None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(UPSERT_GUILD_RADIO_STATEMENT, (guild_id, channel_id, station_link, always_shuffle))
        # Throws an BusyError if not done like this.
        rows = list(cursor)
        return GuildRadioInfo.from_row(rows[0]) if rows[0] else None


def resolve_path_with_links(path: Path, folder: bool = False) -> Path:
    """Resolve a path strictly with more secure default permissions, creating the path if necessary.

    Python only resolves with strict=True if the path exists.

    Source: https://github.com/mikeshardmind/discord-rolebot/blob/4374149bc75d5a0768d219101b4dc7bff3b9e38e/rolebot.py#L350
    """

    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(mode=0o700)  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


async def create_track_embed(title: str, track: wavelink.Playable) -> discord.Embed:
    """Modify an embed to show information about a Wavelink track."""

    icon = MUSIC_EMOJIS.get(track.source, "\N{MUSICAL NOTE}")
    title = f"{icon} {title}"
    uri = track.uri or ""
    author = escape_markdown(track.author)
    track_title = escape_markdown(track.title)

    try:
        end_time = timedelta(seconds=track.length // 1000)
    except OverflowError:
        end_time = "\N{INFINITY}"

    description = f"[{track_title}]({uri})\n{author}\n`[0:00-{end_time}]`"

    embed = discord.Embed(color=0x0389DA, title=title, description=description)

    if track.artwork:
        embed.set_thumbnail(url=track.artwork)

    if track.album.name:
        embed.add_field(name="Album", value=track.album.name)

    return embed


@app_commands.command()
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def radio_set(
    itx: discord.Interaction[RadioBot],
    channel: discord.VoiceChannel | discord.StageChannel,
    station_link: str,
    always_shuffle: bool = True,
) -> None:
    """Create or update your server's radio player, specifically its location and what it will play.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    channel : discord.VoiceChannel | discord.StageChannel
        The channel the radio should automatically play in and, if necessary, reconnect to.
    station_link : str
        The 'radio station' you want to play in your server, e.g. a link to a playlist/audio stream.
    always_shuffle : bool, optional
        Whether the station should shuffle its internal playlist whenever it loops. By default True.
    """

    assert itx.guild  # Known at runtime.

    record = await itx.client.save_radio(
        guild_id=itx.guild.id,
        channel_id=channel.id,
        station_link=station_link,
        always_shuffle=always_shuffle,
    )

    if record:
        content = f"Radio with station {record.station_link} set in <#{record.channel_id}>."
    else:
        content = f"Unable to set radio in {channel.mention} with [this station]({station_link}) at this time."
    await itx.response.send_message(content)


@app_commands.command()
@app_commands.guild_only()
async def radio_get(itx: discord.Interaction[RadioBot]) -> None:
    """Get information about your server's current radio setup. May need /restart to be up to date."""

    assert itx.guild_id  # Known at runtime.

    local_radio_results = _query(itx.client.db_connection, [(itx.guild_id,)])

    if local_radio_results and (local_radio := local_radio_results[0]):
        await itx.response.send_message(embed=local_radio.display_embed())
    else:
        await itx.response.send_message("No radio found for this guild.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def radio_delete(itx: discord.Interaction[RadioBot]) -> None:
    """Delete the radio for the current guild. May need /restart to be up to date."""

    assert itx.guild_id  # Known at runtime.

    await itx.client.delete_radio(itx.guild_id)
    await itx.response.send_message("If this guild had a radio, it has now been deleted.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def radio_restart(itx: discord.Interaction[RadioBot]) -> None:
    """Restart your server's radio. Acts as a reset in case you change something."""

    assert itx.guild  # Known at runtime.

    if vc := itx.guild.voice_client:
        await vc.disconnect(force=True)

    guild_radio_records = _query(itx.client.db_connection, [(itx.guild.id,)])

    if guild_radio_records:
        await itx.response.send_message("Restarting radio now. Give it a few seconds to rejoin.")
    else:
        await itx.response.send_message("This server's radio does not exist. Not restarting.")


@app_commands.command()
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def radio_next(itx: discord.Interaction[RadioBot]) -> None:
    """Skip to the next track. If managing roles are set, only members with those can use this command."""

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        await vc.stop()
        await itx.response.send_message("Skipping to next track.")
    else:
        await itx.response.send_message("No radio currently active in this server.")


@app_commands.command()
@app_commands.guild_only()
async def current(itx: discord.Interaction[RadioBot], level: Literal["track", "radio"] = "track") -> None:
    """See what's currently playing on the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    level : Literal["track", "station", "radio"], optional
        What to get information about: the currently playing track, station, or radio. By default, "track".
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        if level == "track":
            if vc.current:
                embed = await create_track_embed("Currently Playing", vc.current)
            else:
                embed = discord.Embed(description="Nothing is currently playing.")
        else:
            embed = vc.radio_info.display_embed()
        await itx.response.send_message(embed=embed, ephemeral=True)
    else:
        await itx.response.send_message("No radio currently active in this server.")


@app_commands.command()
@app_commands.guild_only()
async def volume(itx: discord.Interaction[RadioBot], volume: int | None = None) -> None:
    """See or change the volume of the radio.

    Parameters
    ----------
    itx : discord.Interaction[RadioBot]
        The interaction that triggered this command.
    volume : int | None, optional
        What to change the volume to, between 1 and 1000. Locked to managing roles if those are set. By default, None.
    """

    # Known at runtime.
    assert itx.guild
    vc = itx.guild.voice_client
    assert isinstance(vc, RadioPlayer | None)

    if vc:
        if volume is None:
            await itx.response.send_message(f"Volume is currently set to {vc.volume}.", ephemeral=True)
        else:
            await vc.set_volume(volume)
            await itx.response.send_message(f"Volume now changed to {vc.volume}.")
    else:
        await itx.response.send_message("No radio currently active in this server.")


@app_commands.command(name="help")
async def _help(itx: discord.Interaction[RadioBot], ephemeral: bool = True) -> None:
    """See a brief overview of all the bot's available commands and basic instructions for setting it up.

    Parameters
    ----------
    itx : :class:`discord.Interaction`
        The interaction that triggered this command.
    ephemeral : :class:`bool`, default=True
        Whether the output should be visible to only you. Defaults to True.
    """

    help_embed = discord.Embed(
        title="Help",
        description=(
            "1. Create the radio for your server with `/radio_set`, using an audio streaming–capable URL "  # noqa: RUF001
            "for the 'station'.\n"
            " - If you want to edit the radio, use the same command. It will require reentering the channel, though.\n"
            "2. The bot should join the channel specified and begin playing shortly!\n\n"
            "`/radio_delete`, `/radio_restart`, and `/radio_next` are restricted by default. To change those usage "
            "permissions, use your server's Integration settings."
        ),
    )

    for cmd in itx.client.tree.walk_commands():
        if isinstance(cmd, app_commands.Command):
            mention = await itx.client.tree.find_mention_for(cmd)
            description = cmd.callback.__doc__ or cmd.description
        else:
            mention = f"/{cmd.name}"
            description = cmd.__doc__ or cmd.description

        try:
            index = description.index("Parameters")
        except ValueError:
            pass
        else:
            description = description[:index]

        help_embed.add_field(name=mention, value=description, inline=False)

    await itx.response.send_message(embed=help_embed, ephemeral=ephemeral)


APP_COMMANDS: list[app_commands.Command[Any, ..., None] | app_commands.Group] = [
    radio_set,
    radio_get,
    radio_delete,
    radio_restart,
    radio_next,
    current,
    volume,
    _help,
]


class RadioPlayer(wavelink.Player):
    """A wavelink player with data about the radio it represents.

    Attributes
    ----------
    radio_info
    """

    @property
    def radio_info(self) -> GuildRadioInfo:
        """`GuildRadioInfo`: A dataclass instance with information about the radio that this player is representing."""

        return self._radio_info

    @radio_info.setter
    def radio_info(self, value: GuildRadioInfo) -> None:
        self._radio_info = value

    async def regenerate_radio_queue(self) -> None:
        """Recreate the queue based on the track link in the player's radio info.

        Raises
        ------
        WavelinkSearchError
            Failed to regenerate the queue.
        """

        self.queue.clear()

        tracks: wavelink.Search = await wavelink.Playable.search(self.radio_info.station_link)
        if not tracks:
            embed = discord.Embed(description="Failed to regenerate the queue")
            embed.add_field(name="Radio link", value=self.radio_info.station_link)
            await self.channel.send(embed=embed)
            return

        if isinstance(tracks, wavelink.Playlist):
            await self.queue.put_wait(tracks)
        else:
            track: wavelink.Playable = tracks[0]
            await self.queue.put_wait(track)

        self.autoplay = wavelink.AutoPlayMode.partial
        self.queue.mode = wavelink.QueueMode.loop_all

        if self.radio_info.always_shuffle:
            self.queue.shuffle()


class VersionableTree(app_commands.CommandTree):
    """A custom command tree to handle autosyncing and save command mentions.

    Credit to LeoCx1000: The implemention for storing mentions of tree commands is his.
    https://gist.github.com/LeoCx1000/021dc52981299b95ea7790416e4f5ca4

    Credit to @mikeshardmind: The hashing methods in this class are his.
    https://github.com/mikeshardmind/discord-rolebot/blob/ff0ca542ccc54a5527935839e511d75d3d178da0/rolebot/__main__.py#L486
    """

    def __init__(self, client: RadioBot, *, fallback_to_global: bool = True) -> None:
        super().__init__(client, fallback_to_global=fallback_to_global)
        self.application_commands: dict[int | None, list[app_commands.AppCommand]] = {}

    async def sync(self, *, guild: discord.abc.Snowflake | None = None) -> list[app_commands.AppCommand]:
        ret = await super().sync(guild=guild)
        self.application_commands[guild.id if guild else None] = ret
        return ret

    async def fetch_commands(
        self,
        *,
        guild: discord.abc.Snowflake | None = None,
    ) -> list[app_commands.AppCommand]:
        ret = await super().fetch_commands(guild=guild)
        self.application_commands[guild.id if guild else None] = ret
        return ret

    async def find_mention_for(
        self,
        command: app_commands.Command[Any, ..., Any] | app_commands.Group | str,
        *,
        guild: discord.abc.Snowflake | None = None,
    ) -> str | None:
        """Retrieves the mention of an AppCommand given a specific command name, and optionally, a guild.

        Parameters
        ----------
        name: app_commands.Command | app_commands.Group | str
            The command which we will attempt to retrieve the mention of.
        guild: discord.abc.Snowflake | None, optional
            The scope (guild) from which to retrieve the commands from. If None is given or not passed,
            the global scope will be used, however, if guild is passed and tree.fallback_to_global is
            set to True (default), then the global scope will also be searched.
        """

        check_global = (self.fallback_to_global is True) or (guild is not None)

        if isinstance(command, str):
            # Try and find a command by that name. discord.py does not return children from tree.get_command, but
            # using walk_commands and utils.get is a simple way around that.
            _command = discord.utils.get(self.walk_commands(guild=guild), qualified_name=command)

            if check_global and not _command:
                _command = discord.utils.get(self.walk_commands(), qualified_name=command)

        else:
            _command = command

        if not _command:
            return None

        if guild:
            try:
                local_commands = self.application_commands[guild.id]
            except KeyError:
                local_commands = await self.fetch_commands(guild=guild)

            app_command_found = discord.utils.get(local_commands, name=(_command.root_parent or _command).name)

        else:
            app_command_found = None

        if check_global and not app_command_found:
            try:
                global_commands = self.application_commands[None]
            except KeyError:
                global_commands = await self.fetch_commands()

            app_command_found = discord.utils.get(global_commands, name=(_command.root_parent or _command).name)

        if not app_command_found:
            return None

        return f"</{_command.qualified_name}:{app_command_found.id}>"

    async def get_hash(self) -> bytes:
        """Generate a unique hash to represent all commands currently in the tree."""

        commands = sorted(self._get_all_commands(guild=None), key=lambda c: c.qualified_name)

        translator = self.translator
        if translator:
            payload = [await command.get_translated_payload(self, translator) for command in commands]
        else:
            payload = [command.to_dict(self) for command in commands]

        return xxhash.xxh3_64_digest(json.dumps(payload).encode("utf-8"), seed=1)

    async def sync_if_commands_updated(self) -> None:
        """Sync the tree globally if its commands are different from the tree's most recent previous version.

        Comparison is done with hashes, with the hash being stored in a specific file if unique for later comparison.

        Notes
        -----
        This uses blocking file IO, so don't run this in situations where that matters. `setup_hook()` should be fine
        a fine place though.
        """

        tree_hash = await self.get_hash()
        tree_hash_path = platformdir_info.user_cache_path / "radiobot_tree.hash"
        tree_hash_path = resolve_path_with_links(tree_hash_path)
        with tree_hash_path.open("r+b") as fp:
            data = fp.read()
            if data != tree_hash:
                _log.info("New version of the command tree. Syncing now.")
                await self.sync()
                fp.seek(0)
                fp.write(tree_hash)


class RadioBot(discord.AutoShardedClient):
    """The Discord client subclass that provides radio-related functionality.

    Parameters
    ----------
    config : :class:`LavalinkCreds`
        The configuration data for the radios, including Lavalink node credentials.

    Attributes
    ----------
    config : :class:`LavalinkCreds`
        The configuration data for the radios, including Lavalink node credentials.
    """

    def __init__(self, config: LavalinkCreds) -> None:
        self.config = config
        super().__init__(
            intents=discord.Intents(guilds=True, voice_states=True, typing=True),
            activity=discord.Game(name="https://github.com/SutaHelmIndustries/discord-radiobot"),
        )
        self.tree = VersionableTree(self)

        # Connect to the database that will store the radio information.
        # -- Need to account for the directories and/or file not existing.
        db_path = platformdir_info.user_data_path / "radiobot_data.db"
        resolved_path_as_str = str(resolve_path_with_links(db_path))
        self.db_connection = apsw.Connection(resolved_path_as_str)

    async def on_connect(self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(274881367040)
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self) -> None:
        """Perform a few operations before the bot connects to the Discord Gateway."""

        # Connect to the Lavalink node that will provide the music.
        node = wavelink.Node(uri=self.config.uri, password=self.config.password)
        await wavelink.Pool.connect(nodes=[node], client=self)

        # Initialize the database and start the loop.
        self._radio_enabled_guilds: set[int] = _setup_db(self.db_connection)
        self.radio_loop.start()

        # Add the app commands to the tree.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

        # Sync the tree if it's different from the previous version, using hashing for comparison.
        await self.tree.sync_if_commands_updated()

    async def close(self) -> None:
        self.radio_loop.cancel()
        await super().close()

    async def start_guild_radio(self, radio_info: GuildRadioInfo) -> None:
        """Create a radio voice client for a guild and start its preset station playlist.

        Parameters
        ----------
        radio_info : GuildRadioInfo
            A dataclass instance with the guild radio's settings.
        """

        # Initialize a guild's radio voice client.
        guild = self.get_guild(radio_info.guild_id)
        if not guild:
            return

        voice_channel = guild.get_channel(radio_info.channel_id)
        assert isinstance(voice_channel, discord.VoiceChannel | discord.StageChannel)

        vc = await voice_channel.connect(cls=RadioPlayer)
        vc.radio_info = radio_info

        # Get the playlist of the guild's registered radio station and play it on loop.
        await vc.regenerate_radio_queue()
        await vc.play(vc.queue.get())

    @tasks.loop(seconds=10.0)
    async def radio_loop(self) -> None:
        """The main loop for the radios.

        It (re)connects voice clients to voice channels and plays preset stations.
        """

        inactive_radio_guild_ids = [
            guild_id
            for guild_id in self._radio_enabled_guilds
            if (guild := self.get_guild(guild_id)) and not guild.voice_client
        ]

        radio_results = _query(self.db_connection, [(guild_id,) for guild_id in inactive_radio_guild_ids])

        for radio in radio_results:
            self.loop.create_task(self.start_guild_radio(radio))

    @radio_loop.before_loop
    async def radio_loop_before(self) -> None:
        await self.wait_until_ready()

    async def save_radio(
        self,
        guild_id: int,
        channel_id: int,
        station_link: str,
        always_shuffle: bool,
    ) -> GuildRadioInfo | None:
        """Create or update a radio.

        Parameters
        ----------
        guild_id : int
            The Discord ID for the guild this radio will be active in.
        channel_id : int
            The Discord ID for the channel this radio will be active in.
        station_link : int
            The URL for a playlist, track, or some other audio stream that will act as the "station".
        always_shuffle : bool
            Whether to always shuffle the station's playlist when the radio starts and as it cycles.

        Returns
        -------
        GuildRadioInfo | None
            A dataclass instance with information about the newly created or updated radio, or None if the operation
            failed.
        """

        record = _add_radio(
            self.db_connection,
            guild_id=guild_id,
            channel_id=channel_id,
            station_link=station_link,
            always_shuffle=always_shuffle,
        )
        self._radio_enabled_guilds.add(guild_id)

        if (guild := self.get_guild(guild_id)) and isinstance((vc := guild.voice_client), RadioPlayer) and record:
            old_record = vc.radio_info
            vc.radio_info = record
            if record.station_link != old_record.station_link:
                await vc.regenerate_radio_queue()

        return record

    async def delete_radio(self, guild_id: int) -> None:
        """Delete a guild's radio.

        Parameters
        ----------
        guild_id : int
            The Discord ID of the guild.
        """

        record = await asyncio.to_thread(_delete, self.db_connection, DELETE_RADIO_BY_GUILD_STATEMENT, (guild_id,))
        self._radio_enabled_guilds.discard(guild_id)

        if (guild := self.get_guild(guild_id)) and (vc := guild.voice_client):
            await vc.disconnect(force=True)

        return record


def _get_stored_credentials(filename: str) -> tuple[str, ...] | None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("r", encoding="utf-8") as fp:
        return tuple(base2048.decode(line.removesuffix("\n")).decode("utf-8") for line in fp.readlines())


def _store_credentials(filename: str, *credentials: str) -> None:
    secret_file_path = platformdir_info.user_config_path / filename
    secret_file_path = resolve_path_with_links(secret_file_path)
    with secret_file_path.open("w", encoding="utf-8") as fp:
        for cred in credentials:
            fp.write(base2048.encode(cred.encode()))
            fp.write("\n")


def _input_token() -> None:
    prompt = "Paste your discord token (won't be visible), then press enter. It will be stored for later use."
    token = getpass.getpass(prompt)
    if not token:
        msg = "Not storing empty token."
        raise RuntimeError(msg)
    _store_credentials("radiobot.token", token)


def _input_lavalink_creds() -> None:
    prompts = (
        "Paste your Lavalink node URI (won't be visible), then press enter. It will be stored for later use.",
        "Paste your Lavalink node password (won't be visible), then press enter. It will be stored for later use.",
    )
    creds: list[str] = []
    for prompt in prompts:
        secret = getpass.getpass(prompt)
        if not secret:
            msg = "Not storing empty lavalink cred."
            raise RuntimeError(msg)
        creds.append(secret)
    _store_credentials("radiobot_lavalink.secrets", *creds)


def _get_token() -> str:
    token = os.getenv("DISCORD_TOKEN") or _get_stored_credentials("radiobot.token")
    if token is None:
        msg = (
            "You're missing a Discord bot token. Use '--token' in the CLI to trigger setup for it, or provide an "
            "environmental variable labelled 'DISCORD_TOKEN'."
        )
        raise RuntimeError(msg)
    return token[0] if isinstance(token, tuple) else token


def _get_lavalink_creds() -> LavalinkCreds:
    if (ll_uri := os.getenv("LAVALINK_URI")) and (ll_pwd := os.getenv("LAVALINK_PASSWORD")):
        lavalink_creds = LavalinkCreds(ll_uri, ll_pwd)
    elif ll_creds := _get_stored_credentials("radiobot_lavalink.secrets"):
        lavalink_creds = LavalinkCreds(ll_creds[0], ll_creds[1])
    else:
        msg = (
            "You're missing Lavalink node credentials. Use '--lavalink' in the CLI to trigger setup for it, or provide "
            "environmental variables labelled 'LAVALINK_URI' and 'LAVALINK_PASSWORD'."
        )
        raise RuntimeError(msg)
    return lavalink_creds


def run_client() -> None:
    """Confirm existence of required credentials and launch the radio bot."""

    async def bot_runner(client: RadioBot) -> None:
        async with client:
            await client.start(token, reconnect=True)

    token = _get_token()
    lavalink_creds = _get_lavalink_creds()

    client = RadioBot(lavalink_creds)

    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner(client))


def main() -> None:
    parser = argparse.ArgumentParser(description="A minimal configuration discord bot for server radios.")
    setup_group = parser.add_argument_group(
        "setup",
        description="Choose credentials to specify. Discord token and Lavalink credentials are required on first run.",
    )
    setup_group.add_argument(
        "--token",
        action="store_true",
        help="Whether to specify the Discord token. Initiates interactive setup.",
        dest="specify_token",
    )
    setup_group.add_argument(
        "--lavalink",
        action="store_true",
        help="Whether you want to specify the Lavalink node URI.",
        dest="specify_lavalink",
    )

    args = parser.parse_args()

    if args.specify_token:
        _input_token()
    if args.specify_lavalink:
        _input_lavalink_creds()

    run_client()


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
