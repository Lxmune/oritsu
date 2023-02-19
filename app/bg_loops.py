from __future__ import annotations

import asyncio
import time
import requests
import sys


import app.packets
import app.settings
import app.state
from app.constants.privileges import Privileges
from app.logging import Ansi
from app.logging import log

from app.constants.gamemodes import GameMode

__all__ = ("initialize_housekeeping_tasks",)

OSU_CLIENT_MIN_PING_INTERVAL = 300000 // 1000  # defined by osu!


async def initialize_housekeeping_tasks() -> None:
    """Create tasks for each housekeeping tasks."""
    log("Initializing housekeeping tasks.", Ansi.LCYAN)

    loop = asyncio.get_running_loop()

    app.state.sessions.housekeeping_tasks.update(
        {
            loop.create_task(task)
            for task in (
                _remove_expired_donation_privileges(interval=30 * 60),
                _update_bot_status(interval=5 * 60),
                _update_online_players_mysql(),
                _update_player_ranks_mysql(),
                _disconnect_ghosts(interval=OSU_CLIENT_MIN_PING_INTERVAL // 3),
            )
        },
    )


async def _remove_expired_donation_privileges(interval: int) -> None:
    """Remove donation privileges from users with expired sessions."""
    while True:
        if app.settings.DEBUG:
            log("Removing expired donation privileges.", Ansi.LMAGENTA)

        expired_donors = await app.state.services.database.fetch_all(
            "SELECT id FROM users "
            "WHERE donor_end <= UNIX_TIMESTAMP() "
            "AND priv & 48",  # 48 = Supporter | Premium
        )

        for expired_donor in expired_donors:
            player = await app.state.sessions.players.from_cache_or_sql(
                id=expired_donor["id"],
            )

            assert player is not None

            # TODO: perhaps make a `revoke_donor` method?
            await player.remove_privs(Privileges.DONATOR)
            player.donor_end = 0
            await app.state.services.database.execute(
                "UPDATE users SET donor_end = 0 WHERE id = :id",
                {"id": player.id},
            )

            if player.online:
                player.enqueue(
                    app.packets.notification("Your supporter status has expired."),
                )

            log(f"{player}'s supporter status has expired.", Ansi.LMAGENTA)

        await asyncio.sleep(interval)


async def _disconnect_ghosts(interval: int) -> None:
    """Actively disconnect users above the
    disconnection time threshold on the osu! server."""
    while True:
        await asyncio.sleep(interval)
        current_time = time.time()

        for player in app.state.sessions.players:
            if current_time - player.last_recv_time > OSU_CLIENT_MIN_PING_INTERVAL:
                log(f"Auto-dced {player}.", Ansi.LMAGENTA)
                player.logout()


async def _update_bot_status(interval: int) -> None:
    """Re roll the bot status, every `interval`."""
    while True:
        await asyncio.sleep(interval)
        app.packets.bot_stats.cache_clear()

async def _update_online_players_mysql() -> None:
    async def function(next_hour):
        _player_count = len(app.state.sessions.players.unrestricted) - 1, 
        # Insert the current time and player count in the online_players table
        await app.state.services.database.execute(
            "INSERT INTO online_players (online, time) VALUES (:online, :time)",
            {"online": _player_count, "time": next_hour},
        )
        log(f"Updated online players table in MySQL with {_player_count} players online.", Ansi.LMAGENTA)

    while True:
        now = time.time()
        next_hour = (int(now / 3600) + 1) * 3600
        time_to_wait = next_hour - now
        await asyncio.sleep(time_to_wait)
        next_hour_formatted = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_hour))
        await function(next_hour_formatted)

# Function that gets the different player's leaderboard ranks (retrieve the rank values in MySQL) and adds the values in the database every 12 hours (table: rank_history, columns: player_id, rank, date )
async def _update_player_ranks_mysql() -> None:
    async def function():
        # Get every players on every gamemode with redis and put it in a list
        ranks_list = []
        for mode in GameMode:
            ranks_list.append(await app.state.services.redis.zrevrange(f"bancho:leaderboard:{mode.value}", 0, -1, withscores=True))

        #ranks_data = await app.state.services.redis.zrevrange("bancho:leaderboard:0", 0, -1, withscores=True)

        # Get the current date
        now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
        start_time = time.time()


        # Loop through every gamemode
        for ranks_data in ranks_list:
            # Loop and insert the current time, player id, rank and pp amount in the rank_history table
            for player_id, pp in ranks_data:
                await app.state.services.database.execute(
                    "INSERT INTO rank_history (player_id, pp, date, `rank`, mode) VALUES (:player_id, :pp, :date, :rank, :mode)",
            {"player_id": player_id, "pp": pp, "date": now, "rank": ranks_data.index((player_id, pp)) + 1, "mode": ranks_list.index(ranks_data)},
                )
            # Message with the elapsed time to insert the data
        log(f"Updated rank history table in MySQL.", Ansi.LMAGENTA)
        log(f"In {time.time() - start_time} seconds.", Ansi.LMAGENTA)
        


    await function()

    # Every 30 minutes
    while True:
        await asyncio.sleep(30 * 60)
        await function()