"""Verify source priority; remote availability rules live in catalog tests."""
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from core import converter
from core.credentials import CredentialManager


class ModelSourceTests(unittest.TestCase):
    def test_local_then_remote_then_default_models(self):
        with (
            patch.object(converter, "_load_models_from_workbuddy", return_value=["local"]) as local,
            patch.object(converter.model_catalog_manager, "get_model_ids", return_value=["remote"]) as remote,
        ):
            self.assertEqual(converter.get_available_models(), ["local"])
            remote.assert_not_called()

            local.return_value = []
            self.assertEqual(converter.get_available_models(), ["remote"])
            remote.assert_called_once_with()

            remote.return_value = []
            self.assertIs(converter.get_available_models(), converter.DEFAULT_MODELS)

    def test_standalone_startup_sets_and_clears_current_credential(self):
        self.addCleanup(CredentialManager.set_current, CredentialManager.current())
        path = Path("synthetic-account.info")
        with (
            patch.dict(converter.CONFIG),
            patch("sys.argv", ["converter", "--skip-check"]),
            patch.object(converter, "find_auth_file", return_value=path) as find,
            patch.object(converter.uvicorn, "run"),
        ):
            converter.main()
            selected = CredentialManager.current()
            self.assertIs(converter.CredentialManager, CredentialManager)
            self.assertEqual(selected.path, path)
            self.assertIs(converter._cred(), selected)
            find.return_value = None
            converter.main()
            self.assertIsNone(CredentialManager.current())
            with self.assertRaises(HTTPException) as error:
                converter._cred()
            self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
