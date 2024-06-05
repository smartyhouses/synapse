#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
import logging
from unittest.mock import patch

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.api.room_versions import RoomVersions
from synapse.rest import admin
from synapse.rest.client import knock, login, room
from synapse.server import HomeServer
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import JsonDict, UserID
from synapse.util import Clock

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)


class GetSyncRoomIdsForUserTestCase(HomeserverTestCase):
    """
    Tests Sliding Sync handler `get_sync_room_ids_for_user()` to make sure it returns
    the correct list of rooms IDs.
    """

    servlets = [
        admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def test_no_rooms(self) -> None:
        """
        Test when the user has never joined any rooms before
        """
        user1_id = self.register_user("user1", "pass")
        # user1_tok = self.login(user1_id, "pass")

        now_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=now_token,
                to_token=now_token,
            )
        )

        self.assertEqual(room_id_results, set())

    def test_get_newly_joined_room(self) -> None:
        """
        Test that rooms that the user has newly_joined show up. newly_joined is when you
        join after the `from_token` and <= `to_token`.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results, {room_id})

    def test_get_already_joined_room(self) -> None:
        """
        Test that rooms that the user is already joined show up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id = self.helper.create_room_as(user1_id, tok=user1_tok, is_public=True)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room_token,
                to_token=after_room_token,
            )
        )

        self.assertEqual(room_id_results, {room_id})

    def test_get_invited_banned_knocked_room(self) -> None:
        """
        Test that rooms that the user is invited to, banned from, and knocked on show
        up.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room_token = self.event_sources.get_current_token()

        # Setup the invited room (user2 invites user1 to the room)
        invited_room_id = self.helper.create_room_as(user2_id, tok=user2_tok)
        self.helper.invite(invited_room_id, targ=user1_id, tok=user2_tok)

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        self.helper.ban(ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Setup the knock room (user1 knocks on the room)
        knock_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, room_version=RoomVersions.V7.identifier
        )
        self.helper.send_state(
            knock_room_id,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=user2_tok,
        )
        # User1 knocks on the room
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/knock/%s" % (knock_room_id,),
            b"{}",
            user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        after_room_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room_token,
                to_token=after_room_token,
            )
        )

        # Ensure that the invited, ban, and knock rooms show up
        self.assertEqual(
            room_id_results,
            {
                invited_room_id,
                ban_room_id,
                knock_room_id,
            },
        )

    def test_get_kicked_room(self) -> None:
        """
        Test that a room that the user was kicked from still shows up. When the user
        comes back to their client, they should see that they were kicked.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        after_kick_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_kick_token,
                to_token=after_kick_token,
            )
        )

        # The kicked room should show up
        self.assertEqual(room_id_results, {kick_room_id})

    def test_forgotten_rooms(self) -> None:
        """
        Forgotten rooms do not show up even if we forget after the from/to range.

        Ideally, we would be able to track when the `/forget` happens and apply it
        accordingly in the token range but the forgotten flag is only an extra bool in
        the `room_memberships` table.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup a normal room that we leave. This won't show up in the sync response
        # because we left it before our token but is good to check anyway.
        leave_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(leave_room_id, user1_id, tok=user1_tok)
        self.helper.leave(leave_room_id, user1_id, tok=user1_tok)

        # Setup the ban room (user2 bans user1 from the room)
        ban_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(ban_room_id, user1_id, tok=user1_tok)
        self.helper.ban(ban_room_id, src=user2_id, targ=user1_id, tok=user2_tok)

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        before_room_forgets = self.event_sources.get_current_token()

        # Forget the room after we already have our tokens. This doesn't change
        # the membership event itself but will mark it internally in Synapse
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{leave_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{ban_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        channel = self.make_request(
            "POST",
            f"/_matrix/client/r0/rooms/{kick_room_id}/forget",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room_forgets,
                to_token=before_room_forgets,
            )
        )

        # We shouldn't see the room because it was forgotten
        self.assertEqual(room_id_results, set())

    def test_only_newly_left_rooms_show_up(self) -> None:
        """
        Test that newly_left rooms still show up in the sync response but rooms that
        were left before the `from_token` don't show up. See condition "2)" comments in
        the `get_sync_room_ids_for_user` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Leave before we calculate the `from_token`
        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave during the from_token/to_token range (newly_left)
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room2_token = self.event_sources.get_current_token()

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room2_token,
            )
        )

        # Only the newly_left room should show up
        self.assertEqual(room_id_results, {room_id2})

    def test_no_joins_after_to_token(self) -> None:
        """
        Rooms we join after the `to_token` should *not* show up. See condition "1b)"
        comments in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Room join after after our `to_token` shouldn't show up
        room_id2 = self.helper.create_room_as(user1_id, tok=user1_tok)
        _ = room_id2

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        self.assertEqual(room_id_results, {room_id1})

    def test_join_during_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined during the
        from_token/to_token. See condition "1a)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined during the
        # from_token/to_token time period.
        self.assertEqual(room_id_results, {room_id1})

    def test_join_before_range_and_left_room_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were joined before the `from_token`
        so it should show up. See condition "1a)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        room_id1 = self.helper.create_room_as(user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # We should still see the room because we were joined before the `from_token`
        self.assertEqual(room_id_results, {room_id1})

    def test_kicked_before_range_and_left_after_to_token(self) -> None:
        """
        Room still shows up if we left the room but were kicked before the `from_token`
        so it should show up. See condition "1a)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Setup the kick room (user2 kicks user1 from the room)
        kick_room_id = self.helper.create_room_as(
            user2_id, tok=user2_tok, is_public=True
        )
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        # Kick user1 from the room
        self.helper.change_membership(
            room=kick_room_id,
            src=user2_id,
            targ=user1_id,
            tok=user2_tok,
            membership=Membership.LEAVE,
            extra_data={
                "reason": "Bad manners",
            },
        )

        after_kick_token = self.event_sources.get_current_token()

        # Leave the room after we already have our tokens
        #
        # We have to join before we can leave (leave -> leave isn't a valid transition
        # or at least it doesn't work in Synapse, 403 forbidden)
        self.helper.join(kick_room_id, user1_id, tok=user1_tok)
        self.helper.leave(kick_room_id, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_kick_token,
                to_token=after_kick_token,
            )
        )

        # We shouldn't see the room because it was forgotten
        self.assertEqual(room_id_results, {kick_room_id})

    def test_newly_left_during_range_and_join_leave_after_to_token(self) -> None:
        """
        Newly left room should show up. But we're also testing that joining and leaving
        after the `to_token` doesn't mess with the results. See condition "2)" and "1a)"
        comments in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room during the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should still show up because it's newly_left during the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_newly_left_during_range_and_join_after_to_token(self) -> None:
        """
        Newly left room should show up. But we're also testing that joining after the
        `to_token` doesn't mess with the results. See condition "2)" and "1b)" comments
        in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room during the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should still show up because it's newly_left during the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_leave_before_range_and_join_leave_after_to_token(self) -> None:
        """
        Old left room shouldn't show up. But we're also testing that joining and leaving
        after the `to_token` doesn't mess with the results. See condition "1a)" comments
        in the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room shouldn't show up because it was left before the `from_token`
        self.assertEqual(room_id_results, set())

    def test_leave_before_range_and_join_after_to_token(self) -> None:
        """
        Old left room shouldn't show up. But we're also testing that joining after the
        `to_token` doesn't mess with the results. See condition "1b)" comments in the
        `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join and leave the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room shouldn't show up because it was left before the `from_token`
        self.assertEqual(room_id_results, set())

    def test_join_leave_multiple_times_during_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times shouldn't affect rooms from showing up. It just
        matters that we were joined or newly_left in the from/to range. But we're also
        testing that joining and leaving after the `to_token` doesn't mess with the
        results.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        before_room1_token = self.event_sources.get_current_token()

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join, leave, join back to the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because it was newly_left and joined during the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_join_leave_multiple_times_before_range_and_after_to_token(
        self,
    ) -> None:
        """
        Join and leave multiple times before the from/to range shouldn't affect rooms
        from showing up. It just matters that we were joined or newly_left in the
        from/to range. But we're also testing that joining and leaving after the
        `to_token` doesn't mess with the results.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        # Join, leave, join back to the room before the from/to range
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Leave and Join the room multiple times after we already have our tokens
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were joined before the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_invite_before_range_and_join_leave_after_to_token(
        self,
    ) -> None:
        """
        Make it look like we joined after the token range but we were invited before the
        from/to range so the room should still show up. See condition "1a)" comments in
        the `get_sync_room_ids_for_user()` method.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Invited to the room before the token
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)

        after_room1_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=after_room1_token,
                to_token=after_room1_token,
            )
        )

        # Room should show up because we were invited before the from/to range
        self.assertEqual(room_id_results, {room_id1})

    def test_multiple_rooms_are_not_confused(
        self,
    ) -> None:
        """
        Test that multiple rooms are not confused as we fixup the list. This test is
        spawning from a real world bug in the code where I was accidentally using
        `event.room_id` in one of the fix-up loops but the `event` being referenced was
        actually from a different loop.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # We create the room with user2 so the room isn't left with no members when we
        # leave and can still re-join.
        room_id1 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        room_id2 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)

        # Invited and left the room before the token
        self.helper.invite(room_id1, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Invited to room2
        self.helper.invite(room_id2, src=user2_id, targ=user1_id, tok=user2_tok)

        before_room3_token = self.event_sources.get_current_token()

        # Invited and left room3 during the from/to range
        room_id3 = self.helper.create_room_as(user2_id, tok=user2_tok, is_public=True)
        self.helper.invite(room_id3, src=user2_id, targ=user1_id, tok=user2_tok)
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        after_room3_token = self.event_sources.get_current_token()

        # Join and leave the room after we already have our tokens
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        # Leave room2
        self.helper.leave(room_id2, user1_id, tok=user1_tok)
        # Leave room3
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_room3_token,
                to_token=after_room3_token,
            )
        )

        self.assertEqual(
            room_id_results,
            {
                # `room_id1` shouldn't show up because we left before the from/to range
                #
                # Room should show up because we were invited before the from/to range
                room_id2,
                # Room should show up because it was newly_left during the from/to range
                room_id3,
            },
        )


class GetSyncRoomIdsForUserEventShardTestCase(BaseMultiWorkerStreamTestCase):
    """
    Tests Sliding Sync handler `get_sync_room_ids_for_user()` to make sure it works with
    sharded event stream_writers enabled
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}

        # Enable shared event stream_writers
        config["stream_writers"] = {"events": ["worker1", "worker2", "worker3"]}
        config["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "worker1": {"host": "testserv", "port": 1001},
            "worker2": {"host": "testserv", "port": 1002},
            "worker3": {"host": "testserv", "port": 1003},
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.sliding_sync_handler = self.hs.get_sliding_sync_handler()
        self.store = self.hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

    def _create_room(self, room_id: str, user_id: str, tok: str) -> None:
        """
        Create a room with a specific room_id. We use this so that that we have a
        consistent room_id across test runs that hashes to the same value and will be
        sharded to a known worker in the tests.
        """

        # We control the room ID generation by patching out the
        # `_generate_room_id` method
        with patch(
            "synapse.handlers.room.RoomCreationHandler._generate_room_id"
        ) as mock:
            mock.side_effect = lambda: room_id
            self.helper.create_room_as(user_id, tok=tok)

    def test_sharded_event_persisters(self) -> None:
        """
        This test should catch bugs that would come from flawed stream position
        (`stream_ordering`) comparisons or making `RoomStreamToken`'s naively. To
        compare event positions properly, you need to consider both the `instance_name`
        and `stream_ordering` together.

        The test creates three event persister workers and a room that is sharded to
        each worker. On worker2, we make the event stream position stuck so that it lags
        behind the other workers and we start getting `RoomStreamToken` that have an
        `instance_map` component (i.e. q`m{min_pos}~{writer1}.{pos1}~{writer2}.{pos2}`).

        We then send some events to advance the stream positions of worker1 and worker3
        but worker2 is lagging behind because it's stuck. We are specifically testing
        that `get_sync_room_ids_for_user(from_token=xxx, to_token=xxx)` should work
        correctly in these adverse conditions.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker1"},
        )

        worker_hs2 = self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker2"},
        )

        self.make_worker_hs(
            "synapse.app.generic_worker",
            {"worker_name": "worker3"},
        )

        # Specially crafted room IDs that get persisted on different workers.
        #
        # Sharded to worker1
        room_id1 = "!fooo:test"
        # Sharded to worker2
        room_id2 = "!bar:test"
        # Sharded to worker3
        room_id3 = "!quux:test"

        # Create rooms on the different workers.
        self._create_room(room_id1, user2_id, user2_tok)
        self._create_room(room_id2, user2_id, user2_tok)
        self._create_room(room_id3, user2_id, user2_tok)
        join_response1 = self.helper.join(room_id1, user1_id, tok=user1_tok)
        join_response2 = self.helper.join(room_id2, user1_id, tok=user1_tok)
        # Leave room2
        self.helper.leave(room_id2, user1_id, tok=user1_tok)
        join_response3 = self.helper.join(room_id3, user1_id, tok=user1_tok)
        # Leave room3
        self.helper.leave(room_id3, user1_id, tok=user1_tok)

        # Ensure that the events were sharded to different workers.
        pos1 = self.get_success(
            self.store.get_position_for_event(join_response1["event_id"])
        )
        self.assertEqual(pos1.instance_name, "worker1")
        pos2 = self.get_success(
            self.store.get_position_for_event(join_response2["event_id"])
        )
        self.assertEqual(pos2.instance_name, "worker2")
        pos3 = self.get_success(
            self.store.get_position_for_event(join_response3["event_id"])
        )
        self.assertEqual(pos3.instance_name, "worker3")

        before_stuck_activity_token = self.event_sources.get_current_token()

        # We now gut wrench into the events stream `MultiWriterIdGenerator` on worker2 to
        # mimic it getting stuck persisting an event. This ensures that when we send an
        # event on worker1/worker3 we end up in a state where worker2 events stream
        # position lags that on worker1/worker3, resulting in a RoomStreamToken with a
        # non-empty `instance_map` component.
        #
        # Worker2's event stream position will not advance until we call `__aexit__`
        # again.
        worker_store2 = worker_hs2.get_datastores().main
        assert isinstance(worker_store2._stream_id_gen, MultiWriterIdGenerator)
        actx = worker_store2._stream_id_gen.get_next()
        self.get_success(actx.__aenter__())

        # For room_id1/worker1: leave and join the room to advance the stream position
        # and generate membership changes.
        self.helper.leave(room_id1, user1_id, tok=user1_tok)
        self.helper.join(room_id1, user1_id, tok=user1_tok)
        # For room_id2/worker2: which is currently stuck, join the room.
        join_on_worker2_response = self.helper.join(room_id2, user1_id, tok=user1_tok)
        # For room_id3/worker3: leave and join the room to advance the stream position
        # and generate membership changes.
        self.helper.leave(room_id3, user1_id, tok=user1_tok)
        join_on_worker3_response = self.helper.join(room_id3, user1_id, tok=user1_tok)

        # Get a token while things are stuck after our activity
        stuck_activity_token = self.event_sources.get_current_token()
        logger.info("stuck_activity_token %s", stuck_activity_token)
        # Let's make sure we're working with a token that has an `instance_map`
        self.assertNotEqual(len(stuck_activity_token.room_key.instance_map), 0)

        # Just double check that the join event on worker2 (that is stuck) happened
        # after the position recorded for worker2 in the token but before the max
        # position in the token. This is crucial for the behavior we're trying to test.
        join_on_worker2_pos = self.get_success(
            self.store.get_position_for_event(join_on_worker2_response["event_id"])
        )
        logger.info("join_on_worker2_pos %s", join_on_worker2_pos)
        # Ensure the join technially came after our token
        self.assertGreater(
            join_on_worker2_pos.stream,
            stuck_activity_token.room_key.get_stream_pos_for_instance("worker2"),
        )
        # But less than the max stream position of some other worker
        self.assertLess(
            join_on_worker2_pos.stream,
            # max
            stuck_activity_token.room_key.get_max_stream_pos(),
        )

        # Just double check that the join event on worker3 happened after the min stream
        # value in the token but still within the position recorded for worker3. This is
        # crucial for the behavior we're trying to test.
        join_on_worker3_pos = self.get_success(
            self.store.get_position_for_event(join_on_worker3_response["event_id"])
        )
        logger.info("join_on_worker3_pos %s", join_on_worker3_pos)
        # Ensure the join came after the min but still encapsulated by the token
        self.assertGreaterEqual(
            join_on_worker3_pos.stream,
            # min
            stuck_activity_token.room_key.stream,
        )
        self.assertLessEqual(
            join_on_worker3_pos.stream,
            stuck_activity_token.room_key.get_stream_pos_for_instance("worker3"),
        )

        # We finish the fake persisting an event we started above and advance worker2's
        # event stream position (unstuck worker2).
        self.get_success(actx.__aexit__(None, None, None))

        # The function under test
        room_id_results = self.get_success(
            self.sliding_sync_handler.get_sync_room_ids_for_user(
                UserID.from_string(user1_id),
                from_token=before_stuck_activity_token,
                to_token=stuck_activity_token,
            )
        )

        self.assertEqual(
            room_id_results,
            {
                room_id1,
                # room_id2 shouldn't show up because we left before the from/to range
                # and the join event during the range happened while worker2 was stuck.
                # This means that from the perspective of the master, where the
                # `stuck_activity_token` is generated, the stream position for worker2
                # wasn't advanced to the join yet. Looking at the `instance_map`, the
                # join technically comes after `stuck_activity_token``.
                #
                # room_id2,
                room_id3,
            },
        )
