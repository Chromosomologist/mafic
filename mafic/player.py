# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections import OrderedDict
from functools import reduce
from logging import getLogger
from operator import or_
from time import time
from typing import TYPE_CHECKING, Generic

from .__libraries import (
    MISSING,
    GuildChannel,
    StageChannel,
    VoiceChannel,
    VoiceProtocol,
)
from .errors import PlayerNotConnected
from .filter import Filter
from .playlist import Playlist
from .pool import NodePool
from .search_type import SearchType
from .track import Track
from .type_variables import ClientT

if TYPE_CHECKING:
    from .__libraries import (
        Connectable,
        Guild,
        GuildVoiceStatePayload,
        VoiceServerUpdatePayload,
    )
    from .node import Node
    from .typings import PlayerUpdateState


_log = getLogger(__name__)
__all__ = ("Player",)


class Player(VoiceProtocol, Generic[ClientT]):
    """Represents a player for a guild.

    .. note::

        This class is not meant to be instantiated by the user.
        Use ``Connectable.connect()`` from your Discord library to connect.

    Parameters
    ----------
    client:
        The client that the player is associated with.
    channel:
        The voice channel to connect to.
    node:
        The node to use for the player. If not provided, the best node will be used.

    Attributes
    ----------
    client: :data:`~mafic.type_variables.ClientT`
        The client that the player is associated with.
    channel:
        The voice channel that the player is connected to.
        This is an ``abc.Connectable`` from your Discord library.
    guild:
        The guild that the player is associated with.
        This is a ``Guild`` from your Discord library.
    """

    def __init__(
        self,
        client: ClientT,
        channel: Connectable,
        *,
        node: Node[ClientT] | None = None,
    ) -> None:
        self.client: ClientT = client
        self.channel: Connectable = channel

        if not isinstance(self.channel, GuildChannel):
            raise TypeError("Voice channel must be a GuildChannel.")

        self.guild: Guild = self.channel.guild

        self._node = node

        self._guild_id: int = self.guild.id
        self._session_id: str | None = None
        self._server_state: VoiceServerUpdatePayload | None = None
        self._connected: bool = False
        self._position: int = 0
        self._last_update: int = 0
        self._ping = -1
        self._current: Track | None = None
        self._filters: OrderedDict[str, Filter] = OrderedDict()

    @property
    def connected(self) -> bool:
        """Whether the player is connected to a voice channel."""

        return self._connected

    @property
    def position(self) -> int:
        """The current position of the player in milliseconds."""

        pos = self._position

        if self._connected and self._current is not None:
            # Add the time since the last update to the position.
            # If the track total time is less than that, use that.
            pos = min(
                self._current.length, pos + int((time() - self._last_update) * 1000)
            )

        return pos

    @property
    def ping(self) -> int:
        """The current ping of the player in milliseconds."""

        return self._ping

    @property
    def node(self) -> Node[ClientT]:
        """The node that the player is connected to."""

        if self._node is None:
            _log.warning(
                "Unable to use best node, player not connected, finding random node.",
                extra={"guild": self._guild_id},
            )
            return NodePool[ClientT].get_random_node()

        return self._node

    def update_state(self, state: PlayerUpdateState) -> None:
        """Update the player state.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        state:
            The state to update the player with.
        """

        self._last_update = state["time"]
        self._position = state.get("position", 0)
        self._connected = state["connected"]
        self._ping = state.get("ping", -1)

    # If people are so in love with the VoiceClient interface
    def is_connected(self) -> bool:
        """Whether the player is connected to a voice channel.

        This is an alias for :attr:`connected`.
        """

        return self._connected

    async def _dispatch_player_update(self) -> None:
        """Dispatch a player update to the node."""

        if self._node is None:
            _log.debug("Recieved voice update before node was found.")
            return

        if self._session_id is None:
            _log.debug("Receieved player update before session ID was set.")
            return

        if self._server_state is None:
            _log.debug("Receieved player update before server state was found")
            return

        await self._node.voice_update(
            guild_id=self._guild_id,
            session_id=self._session_id,
            data=self._server_state,
        )

    async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
        """Dispatch a voice state update.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The voice state update payload.
        """

        before_session_id = self._session_id
        self._session_id = data["session_id"]

        channel_id = data["channel_id"]
        channel = self.guild.get_channel(int(channel_id))
        assert isinstance(channel, (StageChannel, VoiceChannel))

        self.channel = channel

        if self._session_id != before_session_id:
            await self._dispatch_player_update()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        """Handle a voice server update.

        This is called by the library and usually should not be called by the user.

        Parameters
        ----------
        data:
            The voice server update payload.
        """

        # Fetch the best node as we either don't know the best one yet.
        # Or the node we were using was not the best one (endpoint optimisation).
        if (
            self._node is None
            or self._server_state is None
            or self._server_state["endpoint"] != data["endpoint"]
        ):
            _log.debug("Getting best node for player", extra={"guild": self._guild_id})
            self._node = NodePool[ClientT].get_node(
                guild_id=data["guild_id"], endpoint=data["endpoint"]
            )
            _log.debug(
                "Got best node for player: %s",
                self._node.label,
                extra={"guild": self._guild_id},
            )

        self._node.players[self._guild_id] = self

        self._guild_id = int(data["guild_id"])
        self._server_state = data

        await self._dispatch_player_update()

    async def connect(
        self,
        *,
        timeout: float,
        reconnect: bool,
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> None:
        """Connect to a voice channel.

        This is called by your Discord library when using ``Connectable.connect``.

        Parameters
        ----------
        timeout:
            The timeout for the connection.
        reconnect:
            Whether to reconnect to the voice channel if the connection is lost.
        self_mute:
            Whether to mute the bot on connect.
        self_deaf:
            Whether to deafen the bot on connect.
        """

        if not isinstance(self.channel, (VoiceChannel, StageChannel)):
            raise TypeError("Voice channel must be a VoiceChannel or StageChannel.")

        _log.debug("Connecting to voice channel %s", self.channel.id)

        await self.channel.guild.change_voice_state(
            channel=self.channel, self_mute=self_mute, self_deaf=self_deaf
        )
        self._connected = True

    async def disconnect(self, *, force: bool = False) -> None:
        """Disconnect from the voice channel.

        Parameters
        ----------
        force:
            Whether to force the disconnect even if not connected.
        """

        if not self._connected and not force:
            return

        try:
            _log.debug(
                "Disconnecting from voice channel.",
                extra={"guild": self._guild_id},
            )
            await self.guild.change_voice_state(channel=None)
        finally:
            self.cleanup()
            self._connected = False

    async def destroy(self) -> None:
        """Destroy the player.

        This will disconnect the player and remove it from the node.
        """

        _log.debug(
            "Disconnecting player and destroying client.",
            extra={"guild": self._guild_id},
        )
        await self.disconnect()

        if self._node is not None:
            self._node.players.pop(self.guild.id, None)
            await self._node.destroy(guild_id=self.guild.id)

    async def fetch_tracks(
        self, query: str, search_type: SearchType | str = SearchType.YOUTUBE
    ) -> list[Track] | Playlist | None:
        """Fetch tracks from the node.

        Parameters
        ----------
        query:
            The query to search for.
        search_type:
            The search type to use.

        Returns
        -------
        :class:`list`\\[:class:`Track`]
            A list of tracks if the load type is ``TRACK_LOADED`` or ``SEARCH_RESULT``.
        :class:`Playlist`
            A playlist if the load type is ``PLAYLIST_LOADED``.
        None
            If the load type is ``NO_MATCHES``.

        Notes
        -----
        If a node was not selected due to not being connected, this will use a
        random node.
        """

        node = self.node

        raw_type: str
        if isinstance(search_type, SearchType):
            raw_type = search_type.value
        else:
            raw_type = search_type

        return await node.fetch_tracks(query, search_type=raw_type)

    async def update(
        self,
        *,
        track: Track | None = MISSING,
        position: int | None = None,
        end_time: int | None = None,
        volume: int | None = None,
        pause: bool | None = None,
        filter: Filter | None = None,
        replace: bool = False,
    ) -> None:
        """Update the player.

        Parameters
        ----------
        track:
            The track to play.
        position:
            The position to start the track at.
        end_time:
            The time to end the track at.
        volume:
            The volume to play the track at.
        pause:
            Whether to pause the track.
        filter:
            The filter to apply to the track.
        replace:
            Whether to replace the current track if one is playing.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """

        if self._node is None or not self._connected:
            raise PlayerNotConnected

        if track is not MISSING:
            self._current = track

        if position is not None:
            self._position = position

        await self._node.update(
            guild_id=self._guild_id,
            track=track,
            position=position,
            end_time=end_time,
            volume=volume,
            pause=pause,
            filter=filter,
            no_replace=not replace,
        )

    async def play(
        self,
        track: Track,
        /,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        volume: int | None = None,
        replace: bool = True,
        pause: bool | None = None,
    ) -> None:
        """Play the given track.

        Parameters
        ----------
        track:
            The track to play.
        start_time:
            The position to start the track at.
        end_time:
            The time to end the track at.
        volume:
            The volume to play the track at.
        replace:
            Whether to replace the current track if one is playing.
        pause:
            Whether to pause the track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """

        return await self.update(
            track=track,
            position=start_time,
            end_time=end_time,
            volume=volume,
            replace=replace,
            pause=pause,
        )

    async def pause(self, pause: bool = True) -> None:
        """Pause the current track.

        Parameters
        ----------
        pause:
            Whether to pause the track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """

        return await self.update(pause=pause)

    async def resume(self) -> None:
        """Resume the current track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`pause`, with ``pause`` set to
        ``False``.
        """

        return await self.pause(False)

    async def stop(self) -> None:
        """Stop the current track.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """

        await self.update(track=None)

    async def _update_filters(self, *, fast_apply: bool) -> None:
        """Update the filters on the player.

        Parameters
        ----------
        fast_apply:
            Whether to seek to the current position after updating the filters.
        """

        await self.update(filter=reduce(or_, self._filters.values()))

        if fast_apply:
            await self.seek(self.position)

    async def add_filter(
        self, filter: Filter, /, *, label: str, fast_apply: bool = False
    ) -> None:
        """Add a filter to the player.

        Parameters
        ----------
        filter:
            The filter to add.
        label:
            The label to use for the filter. This is used to remove the filter
            later.
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """

        self._filters[label] = filter

        await self._update_filters(fast_apply=fast_apply)

    async def remove_filter(self, label: str, *, fast_apply: bool = False) -> None:
        """Remove a filter from the player.

        Parameters
        ----------
        label:
            The label of the filter to remove.
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        ValueError
            If the filter does not exist.
        """

        self._filters.pop(label)

        await self._update_filters(fast_apply=fast_apply)

    async def clear_filters(self, *, fast_apply: bool = False) -> None:
        """Remove all filters from the player.

        Parameters
        ----------
        fast_apply:
            Whether to seek to the current position after updating the filters.
            This clears Lavalink's internal buffer, which may cause a small
            delay in the audio.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.
        """

        self._filters.clear()

        await self._update_filters(fast_apply=fast_apply)

    async def set_volume(self, volume: int, /) -> None:
        """Set the volume of the player.

        Parameters
        ----------
        volume:
            The volume to set.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """

        await self.update(volume=volume)

    async def seek(self, position: int, /) -> None:
        """Seek to a position in the current track.

        Parameters
        ----------
        position:
            The position to seek to, in milliseconds.

        Raises
        ------
        PlayerNotConnected
            If the player is not connected to a voice channel.

        Notes
        -----
        This is a convenience method for :meth:`update`.
        """

        return await self.update(position=position)
