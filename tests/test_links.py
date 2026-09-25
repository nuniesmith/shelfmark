from __future__ import annotations

import hashlib
import hmac
import base64
import unittest

from src.shelfmark_service import links

TOKEN = "api-token-for-tests"
BOOK = "0123456789abcdef01234567"
NOW = 1_800_000_000


class SignAndVerifyTests(unittest.TestCase):
    """The token is the only thing standing between a link and the file, so
    every way of altering one must be refused -- and an expired genuine one
    told apart from a forgery, because its holder can just ask again."""

    def test_a_signed_token_grants_exactly_its_ebook(self) -> None:
        token = links.sign(TOKEN, BOOK, NOW + 60)
        self.assertEqual(links.verify(TOKEN, token, now=NOW), BOOK)

    def test_an_expired_token_is_refused_as_expired(self) -> None:
        token = links.sign(TOKEN, BOOK, NOW - 1)
        with self.assertRaises(links.LinkExpired):
            links.verify(TOKEN, token, now=NOW)

    def test_the_expiry_second_itself_is_already_expired(self) -> None:
        token = links.sign(TOKEN, BOOK, NOW)
        with self.assertRaises(links.LinkExpired):
            links.verify(TOKEN, token, now=NOW)

    def test_pointing_a_token_at_another_ebook_breaks_its_signature(self) -> None:
        version, _book, expires, signature = links.sign(TOKEN, BOOK, NOW + 60).split(".")
        forged = ".".join([version, "f" * 24, expires, signature])
        with self.assertRaises(links.LinkError) as caught:
            links.verify(TOKEN, forged, now=NOW)
        self.assertNotIsInstance(caught.exception, links.LinkExpired)

    def test_extending_a_token_breaks_its_signature(self) -> None:
        version, book, _expires, signature = links.sign(TOKEN, BOOK, NOW - 60).split(".")
        forged = ".".join([version, book, str(NOW + 10**6), signature])
        with self.assertRaises(links.LinkError) as caught:
            links.verify(TOKEN, forged, now=NOW)
        # Refused for the signature, BEFORE the expiry is even looked at.
        self.assertNotIsInstance(caught.exception, links.LinkExpired)

    def test_a_forged_expired_token_does_not_learn_that_it_expired(self) -> None:
        forged = f"v1.{BOOK}.{NOW - 5}.{'A' * 43}"
        with self.assertRaises(links.LinkError) as caught:
            links.verify(TOKEN, forged, now=NOW)
        self.assertNotIsInstance(caught.exception, links.LinkExpired)

    def test_rotating_the_api_token_revokes_every_link(self) -> None:
        token = links.sign(TOKEN, BOOK, NOW + 60)
        with self.assertRaises(links.LinkError):
            links.verify("a-new-api-token", token, now=NOW)

    def test_the_api_token_itself_is_not_the_signing_key(self) -> None:
        """A second use of the API token as an HMAC key (anywhere, ever) must
        not be able to mint download links."""
        message = f"v1.{BOOK}.{NOW + 60}"
        digest = hmac.new(TOKEN.encode(), message.encode(), hashlib.sha256).digest()
        naive = message + "." + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        with self.assertRaises(links.LinkError):
            links.verify(TOKEN, naive, now=NOW)

    def test_no_api_token_means_no_links_either_way(self) -> None:
        with self.assertRaises(ValueError):
            links.sign("", BOOK, NOW + 60)
        with self.assertRaises(links.LinkError):
            links.verify("", links.sign(TOKEN, BOOK, NOW + 60), now=NOW)

    def test_malformed_tokens_are_refused_not_crashed_on(self) -> None:
        good = links.sign(TOKEN, BOOK, NOW + 60)
        for bad in [
            "",
            "v1",
            good + ".extra",
            good.replace("v1.", "v2.", 1),
            f"v1.{BOOK}.soon.{good.rsplit('.', 1)[1]}",
            f"v1.../../etc/passwd.{NOW + 60}.{good.rsplit('.', 1)[1]}",
            f"v1.{BOOK}.{NOW + 60}.ščž",  # non-ASCII must not reach compare_digest as str
            f"v1.{BOOK}.{'9' * 40}.{good.rsplit('.', 1)[1]}",
        ]:
            with self.subTest(token=bad), self.assertRaises(links.LinkError):
                links.verify(TOKEN, bad, now=NOW)

    def test_only_real_ebook_ids_can_be_signed(self) -> None:
        with self.assertRaises(ValueError):
            links.sign(TOKEN, "../../etc/passwd", NOW + 60)


if __name__ == "__main__":
    unittest.main()
