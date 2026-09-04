from django.test import SimpleTestCase

from apps.common.schema import _clean_dump


class CleanDumpTests(SimpleTestCase):
    def test_strips_psql_meta_and_billing(self):
        raw = r"""
\restrict abc
SET transaction_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
CREATE TABLE public.users (id uuid NOT NULL);
CREATE TABLE public.billing_plans (id uuid NOT NULL);
ALTER TABLE ONLY public.billing_plans ADD CONSTRAINT billing_plans_pkey PRIMARY KEY (id);
CREATE TABLE public.organizations (id uuid NOT NULL);
CREATE TABLE public.django_migrations (id integer NOT NULL);
CREATE TABLE public.auth_user (id integer NOT NULL);
\unrestrict abc
"""
        sql = _clean_dump(raw)
        self.assertIn("users", sql)
        self.assertIn("organizations", sql)
        self.assertNotIn("billing_plans", sql)
        self.assertNotIn("\\restrict", sql)
        self.assertNotIn("transaction_timeout", sql)
        self.assertNotIn("search_path", sql)
        self.assertNotIn("django_migrations", sql)
        self.assertNotIn("auth_user", sql)
