import unittest

from autosport.continuous_session import ContinuousSessionCoordinator
from autosport.session import AutosportSession


class SettlementConsumerSubclassDispatchTests(unittest.TestCase):
    def test_continuous_session_class_body_cannot_shadow_settlement_consumer(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable|canonical settlement coordinator is not extensible",
        ):

            class ForgedContinuousSession(ContinuousSessionCoordinator):
                def _settle(self, *, resolutions):
                    return ("forged-ticket",), ("forged-evidence",)

    def test_autosport_session_class_body_cannot_shadow_settlement_consumer(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):

            class ForgedAutosportSession(AutosportSession):
                def _run_dataset_locked(self, *args, **kwargs):
                    return "forged-result"

    def test_continuous_session_clean_subclass_cannot_later_install_shadow(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement coordinator is not extensible",
        ):

            class CleanContinuousSession(ContinuousSessionCoordinator):
                pass

    def test_autosport_session_clean_subclass_cannot_later_install_shadow(self) -> None:
        class CleanAutosportSession(AutosportSession):
            pass

        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            CleanAutosportSession._run_dataset_locked = lambda self, *args, **kwargs: None

        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            del CleanAutosportSession._run_dataset_locked

        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            CleanAutosportSession._settlement_consumer_bindings_sealed = False

    def test_continuous_settlement_consumer_metaclass_cannot_be_extended(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer metaclass is not extensible",
        ):

            class ForgedContinuousMeta(type(ContinuousSessionCoordinator)):
                pass

    def test_autosport_settlement_consumer_metaclass_cannot_be_extended(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer metaclass is not extensible",
        ):

            class ForgedAutosportMeta(type(AutosportSession)):
                pass


if __name__ == "__main__":
    unittest.main()