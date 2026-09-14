"""Outbound endpoint boundary tests, isolated from DNS and provider services."""
import unittest
from unittest.mock import patch

from src.agents.llm_client import validate_public_endpoint


class EndpointSecurityTests(unittest.TestCase):
    def test_admin_endpoints_reject_anonymous_and_incorrect_tokens(self):
        from fastapi.testclient import TestClient
        from src.web.app import app
        client = TestClient(app)
        with patch.dict('os.environ', {'TRAFFIC_ADMIN_TOKEN': 'test-token'}):
            for endpoint in ('/api/llm/config', '/api/llm/detect-models'):
                for headers in ({}, {'Authorization': 'Bearer incorrect'}):
                    with self.subTest(endpoint=endpoint, headers=headers):
                        self.assertEqual(client.post(endpoint, json={}, headers=headers).status_code, 401)
        with patch.dict('os.environ', {'TRAFFIC_ADMIN_TOKEN': ''}):
            self.assertEqual(client.post('/api/llm/config', json={}).status_code, 503)

    def test_unlisted_origin_is_rejected(self):
        from fastapi.testclient import TestClient
        from src.web.app import app
        response = TestClient(app).options('/api/llm/config', headers={
            'Origin': 'https://untrusted.invalid', 'Access-Control-Request-Method': 'POST'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_rejects_insecure_or_credential_bearing_urls_before_dns(self):
        with patch('src.agents.llm_client.socket.getaddrinfo') as resolve:
            for url in ('http://127.0.0.1/v1', 'file:///etc/passwd',
                        'https://user:password@example.com', 'https://example.com:8443',
                        'https://example.com?api_key=secret'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validate_public_endpoint(url)
            resolve.assert_not_called()

    def test_rejects_any_private_address_in_dns_answer(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
            records = [(2, 1, 6, '', ('8.8.8.8', 443)), (2, 1, 6, '', (address, 443))]
            with self.subTest(address=address), patch(
                    'src.agents.llm_client.socket.getaddrinfo', return_value=records):
                with self.assertRaises(ValueError):
                    validate_public_endpoint('https://provider.example/v1')

    def test_accepts_public_https_provider(self):
        with patch('src.agents.llm_client.socket.getaddrinfo',
                   return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]):
            validate_public_endpoint('https://provider.example/v1')
