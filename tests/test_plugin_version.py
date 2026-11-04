"""The Plugin entry carries its own version; the full product keeps PRODUCT_VERSION.

The two numbers are equal today, so every assertion that they are separate patches one of
them: a test that only compared equal constants would pass with the coupling still in place.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from garmin_coach_loop import gateway as gateway_module
from garmin_coach_loop.gateway import (
    PLUGIN_VERSION,
    PRODUCT_VERSION,
    CoachGateway,
    CoachGatewayHandler,
    CoachGatewayServer,
    GatewayConfig,
)

HMAC_KEY = b"k" * 32
UNRELEASED = "9.9.9"


class PluginVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, **extra) -> GatewayConfig:
        return GatewayConfig(
            state_root=self.root, token_hmac_key=HMAC_KEY,
            intervals_client_id="client", intervals_client_secret="secret", **extra,
        )

    def test_each_product_serves_its_own_number(self):
        self.assertEqual(PRODUCT_VERSION, self.config().served_version)
        self.assertEqual(PLUGIN_VERSION, self.config(product="training").served_version)

    def test_a_plugin_release_does_not_move_the_full_product(self):
        with mock.patch.object(gateway_module, "PLUGIN_VERSION", UNRELEASED):
            self.assertEqual(UNRELEASED, self.config(product="training").served_version)
            self.assertEqual(PRODUCT_VERSION, self.config().served_version)
            # Health data is a separate switch from the product; it does not pick a number.
            self.assertEqual(
                UNRELEASED, self.config(product="training", health_data=True).served_version
            )

    def test_a_full_product_release_does_not_move_the_plugin(self):
        with mock.patch.object(gateway_module, "PRODUCT_VERSION", UNRELEASED):
            self.assertEqual(UNRELEASED, self.config().served_version)
            self.assertEqual(PLUGIN_VERSION, self.config(product="training").served_version)

    def test_readyz_states_the_plugin_number_on_a_training_deployment(self):
        with mock.patch.object(gateway_module, "PLUGIN_VERSION", UNRELEASED):
            training = CoachGateway(self.config(product="training")).health()
            self.assertEqual(UNRELEASED, training["product_version"])
            self.assertEqual("training", training["deployment_product"])
        hybrid = CoachGateway(self.config()).health()
        self.assertEqual(PRODUCT_VERSION, hybrid["product_version"])

    def test_mcp_initialize_is_built_with_the_same_number_readyz_states(self):
        with mock.patch.object(gateway_module, "PLUGIN_VERSION", UNRELEASED), \
                mock.patch.object(gateway_module.mcp_sdk_transport, "SdkTransport") as transport:
            gateway = CoachGateway(self.config(product="training"))
            server = CoachGatewayServer(("127.0.0.1", 0), CoachGatewayHandler, gateway=gateway)
            server.server_close()
        transport.assert_called_once_with(UNRELEASED, gateway.surface)


if __name__ == "__main__":
    unittest.main()
