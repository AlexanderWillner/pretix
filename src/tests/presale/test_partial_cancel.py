#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#

"""
Tests for partial ticket cancellation of free orders.

This test file focuses on the scenario where a free order with multiple
positions is partially canceled (i.e., only one position is canceled while
the other remains active). This scenario is relevant for verifying that
the order change mechanism correctly handles partial cancellations.
"""

import datetime
from decimal import Decimal

import pytest
from django.test import TestCase
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import (
    Event, Item, ItemCategory, Order, OrderPosition, Organizer, Quota,
)
from pretix.base.models.orders import OrderPayment
from pretix.base.services.orders import OrderChangeManager, OrderError


class PartialCancelFreeOrderTest(TestCase):
    """
    Test partial cancellation of free orders with multiple positions.

    This test creates a free order (total = 0) with two order positions,
    then attempts to cancel only one position via the OrderChangeManager.
    The expected outcome is that:
    - One position remains active
    - One position is marked as canceled
    - The order itself remains valid
    """

    @scopes_disabled()
    def setUp(self):
        super().setUp()
        self.orga = Organizer.objects.create(
            name='TestOrga',
            slug='testorga',
            plugins='pretix.plugins.banktransfer'
        )
        self.event = Event.objects.create(
            organizer=self.orga,
            name='Test Event',
            slug='testevent',
            date_from=datetime.datetime(2024, 12, 26, tzinfo=datetime.timezone.utc),
            plugins='pretix.plugins.banktransfer',
            live=True
        )
        self.event.settings.set('payment_banktransfer__enabled', True)

        self.category = ItemCategory.objects.create(
            event=self.event,
            name="Tickets",
            position=0
        )
        self.quota = Quota.objects.create(
            event=self.event,
            name='Test Quota',
            size=100
        )
        # Create a free ticket item (price = 0)
        self.free_ticket = Item.objects.create(
            event=self.event,
            name='Free Ticket',
            category=self.category,
            default_price=Decimal('0.00'),
            admission=True
        )
        self.quota.items.add(self.free_ticket)

        # Create a free order with two positions
        self.order = Order.objects.create(
            status=Order.STATUS_PAID,  # Free orders are immediately paid
            event=self.event,
            email='test@example.com',
            datetime=now() - datetime.timedelta(days=1),
            expires=now() + datetime.timedelta(days=30),
            total=Decimal('0.00'),
            sales_channel=self.orga.sales_channels.get(identifier="web"),
            locale='en'
        )
        # Create first order position
        self.position1 = OrderPosition.objects.create(
            order=self.order,
            item=self.free_ticket,
            variation=None,
            price=Decimal('0.00'),
            attendee_name_parts={'full_name': 'Alice'}
        )
        # Create second order position
        self.position2 = OrderPosition.objects.create(
            order=self.order,
            item=self.free_ticket,
            variation=None,
            price=Decimal('0.00'),
            attendee_name_parts={'full_name': 'Bob'}
        )
        # Mark order as paid with a free payment
        OrderPayment.objects.create(
            order=self.order,
            provider='free',
            amount=Decimal('0.00'),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED
        )

    def test_partial_cancel_free_order_one_position(self):
        """
        Test canceling one position of a free order with two positions.

        This test verifies that:
        1. A free order with two positions can have one position canceled
        2. After partial cancellation, the remaining position is still active
        3. The canceled position is marked as canceled
        4. The order remains in a valid state
        """
        with scopes_disabled():
            # Verify initial state
            assert self.order.status == Order.STATUS_PAID
            assert self.order.total == Decimal('0.00')
            assert self.order.positions.filter(canceled=False).count() == 2

            # Create OrderChangeManager to perform partial cancellation
            ocm = OrderChangeManager(
                order=self.order,
                notify=False,
                reissue_invoice=False,
            )

            # Cancel only the first position
            ocm.cancel(self.position1)
            ocm.commit()

            # Refresh from database
            self.order.refresh_from_db()
            self.position1.refresh_from_db()
            self.position2.refresh_from_db()

            # Assert that position1 is canceled
            assert self.position1.canceled, \
                "Position 1 should be marked as canceled"

            # Assert that position2 is still active
            assert not self.position2.canceled, \
                "Position 2 should remain active"

            # Assert the order still has one active position
            active_positions = self.order.positions.filter(canceled=False)
            assert active_positions.count() == 1, \
                "Order should have exactly one active position after partial cancel"
            assert active_positions.first().attendee_name == 'Bob', \
                "The remaining active position should be Bob's ticket"

            # Assert the order total remains 0
            assert self.order.total == Decimal('0.00'), \
                "Order total should remain 0 for free order"

    def test_partial_cancel_preserves_order_status(self):
        """
        Test that partial cancellation of a free order preserves order status.

        For a paid free order, partially canceling positions should not change
        the order status if there are still active positions remaining.
        """
        with scopes_disabled():
            original_status = self.order.status

            ocm = OrderChangeManager(
                order=self.order,
                notify=False,
                reissue_invoice=False,
            )

            # Cancel only position2 this time
            ocm.cancel(self.position2)
            ocm.commit()

            self.order.refresh_from_db()

            # Order should still be paid as there's still one active position
            assert self.order.status == original_status, \
                f"Order status should remain {original_status} after partial cancel"

            # Verify the correct position is canceled
            self.position2.refresh_from_db()
            assert self.position2.canceled

    def test_cancel_all_positions_raises_error(self):
        """
        Test that attempting to cancel all positions raises an error.

        The OrderChangeManager should not allow canceling all positions
        of an order. If all positions need to be canceled, the order
        itself should be canceled instead.
        """
        with scopes_disabled():
            ocm = OrderChangeManager(
                order=self.order,
                notify=False,
                reissue_invoice=False,
            )

            # Cancel both positions
            ocm.cancel(self.position1)
            ocm.cancel(self.position2)

            # Attempting to commit should raise an OrderError
            with pytest.raises(OrderError) as exc_info:
                ocm.commit()

            # Verify the error message indicates complete cancel is not allowed
            assert "empty" in str(exc_info.value).lower() or "cancel" in str(exc_info.value).lower(), \
                "Error should indicate that complete cancellation via positions is not allowed"

            # Verify positions were not actually canceled
            self.position1.refresh_from_db()
            self.position2.refresh_from_db()
            assert not self.position1.canceled, \
                "Position 1 should not be canceled after failed commit"
            assert not self.position2.canceled, \
                "Position 2 should not be canceled after failed commit"
