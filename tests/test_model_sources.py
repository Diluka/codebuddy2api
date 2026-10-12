"""Verify source priority; remote availability rules live in catalog tests."""
import unittest
from unittest.mock import patch

from core import converter


class ModelSourceTests(unittest.TestCase):
    def test_local_then_remote_then_default_models(self):
        credential = object()
        with (
            patch.dict(converter.CONFIG, {"cred": credential}),
            patch.object(converter, "_load_models_from_workbuddy", return_value=["local"]) as local,
            patch.object(converter.model_catalog_manager, "get_model_ids", return_value=["remote"]) as remote,
        ):
            self.assertEqual(converter.get_available_models(), ["local"])
            remote.assert_not_called()

            local.return_value = []
            self.assertEqual(converter.get_available_models(), ["remote"])
            remote.assert_called_once_with(credential)

            remote.return_value = []
            self.assertIs(converter.get_available_models(), converter.DEFAULT_MODELS)


if __name__ == "__main__":
    unittest.main()
