from django.test import SimpleTestCase

from apps.common.dburl import neon_direct_host, parse_database_url, with_direct_host


class DatabaseUrlTests(SimpleTestCase):
    def test_strips_neon_pooler_host(self):
        url = (
            "postgresql://neondb_owner:secret@ep-example-pooler.c-2.us-east-2.aws.neon.tech"
            "/neondb?sslmode=require&channel_binding=require"
        )
        parsed = parse_database_url(url)
        self.assertEqual(parsed["HOST"], "ep-example-pooler.c-2.us-east-2.aws.neon.tech")
        self.assertEqual(parsed["NAME"], "neondb")
        self.assertEqual(parsed["USER"], "neondb_owner")
        self.assertEqual(parsed["PASSWORD"], "secret")
        self.assertTrue(parsed["DISABLE_SERVER_SIDE_CURSORS"])
        self.assertEqual(parsed["OPTIONS"]["sslmode"], "require")

        direct = with_direct_host(parsed)
        self.assertEqual(direct["HOST"], "ep-example.c-2.us-east-2.aws.neon.tech")
        self.assertFalse(direct["DISABLE_SERVER_SIDE_CURSORS"])

    def test_neon_direct_host_noop(self):
        self.assertEqual(neon_direct_host("127.0.0.1"), "127.0.0.1")
