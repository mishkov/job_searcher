import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from telethon.extensions import markdown
from telethon.tl.types import Message, MessageEntityBold, PeerChannel

from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSource, TelegramChannelJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 26, tzinfo=timezone.utc)
PROMOTION = 'Flutter & Dart | Мобильный трудоголик - Канал про разработку на Flutter.'


class TelegramChannelTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, text, filters=None, fragments=None, entities=None, date=CUTOFF):
        message = Message(id=123, peer_id=PeerChannel(456), date=date, message=text, entities=entities)

        async def messages():
            yield message

        client = Mock(iter_messages=Mock(return_value=messages()), parse_mode=markdown)
        message._client = client
        config = TelegramChannelJobsSourceConfig(
            'example', filters or SearchFilter(['flutter']), ignored_text_fragments=fragments,
        )
        source = TelegramChannelJobsSource(client, config)
        with patch.object(source, '_get_message_url', new=AsyncMock(return_value='https://t.me/example/123')):
            return await source.fetch(CUTOFF), message

    async def test_keyword_only_in_ignored_promotion_does_not_match(self):
        jobs, _ = await self.fetch('IOS разработчик\n' + PROMOTION, fragments=[PROMOTION])
        self.assertEqual(jobs, [])

    async def test_keyword_outside_promotion_matches_and_keeps_original_message(self):
        jobs, message = await self.fetch(
            'Flutter разработчик\n' + PROMOTION, fragments=[PROMOTION],
            entities=[MessageEntityBold(offset=0, length=7)],
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].description, message.text)
        self.assertIn(PROMOTION, jobs[0].description)
        self.assertIn('**Flutter**', jobs[0].description)
        self.assertEqual(jobs[0].url, 'https://t.me/example/123')

    async def test_message_without_promotion_matches(self):
        jobs, _ = await self.fetch('Flutter разработчик', fragments=[PROMOTION])
        self.assertEqual(len(jobs), 1)

    async def test_no_fragments_preserves_keyword_matching_in_footer(self):
        jobs, _ = await self.fetch('IOS разработчик\n' + PROMOTION)
        self.assertEqual(len(jobs), 1)

    async def test_fragments_and_keywords_ignore_case(self):
        jobs, _ = await self.fetch('iOS\n' + PROMOTION.upper(), fragments=[PROMOTION.lower()])
        self.assertEqual(jobs, [])
        jobs, _ = await self.fetch('FLUTTER\n' + PROMOTION, fragments=[PROMOTION])
        self.assertEqual(len(jobs), 1)

    async def test_bold_entities_do_not_prevent_plain_text_fragment_matching(self):
        text = 'IOS\n' + PROMOTION
        jobs, _ = await self.fetch(
            text, fragments=[PROMOTION],
            entities=[MessageEntityBold(offset=4, length=len('Flutter & Dart | Мобильный трудоголик'))],
        )
        self.assertEqual(jobs, [])

    async def test_excluded_keyword_in_ignored_fragment_does_not_reject_job(self):
        jobs, _ = await self.fetch(
            'iOS разработчик\n' + PROMOTION, SearchFilter(['ios'], ['flutter']), [PROMOTION],
        )
        self.assertEqual(len(jobs), 1)

    async def test_excluded_keyword_outside_fragment_still_takes_priority(self):
        for filters in (SearchFilter(['ios'], ['flutter']), SearchFilter(['*'], ['flutter'])):
            with self.subTest(filters=filters):
                jobs, _ = await self.fetch('iOS and Flutter\n' + PROMOTION, filters, [PROMOTION])
                self.assertEqual(jobs, [])

    async def test_multiple_fragments_and_repeated_occurrences_are_removed(self):
        jobs, _ = await self.fetch(
            PROMOTION + '\nFlutter news\n' + PROMOTION, fragments=[PROMOTION, 'Flutter news'],
        )
        self.assertEqual(jobs, [])

    async def test_removing_fragment_does_not_join_surrounding_words(self):
        jobs, _ = await self.fetch('flu[promotion]tter', fragments=['[promotion]'])
        self.assertEqual(jobs, [])

    async def test_wildcard_still_matches_when_all_text_is_ignored(self):
        jobs, _ = await self.fetch(PROMOTION, SearchFilter(), [PROMOTION])
        self.assertEqual(len(jobs), 1)

    async def test_empty_and_old_messages_are_skipped(self):
        jobs, _ = await self.fetch(None, SearchFilter())
        self.assertEqual(jobs, [])
        jobs, _ = await self.fetch('Flutter', date=CUTOFF - timedelta(seconds=1))
        self.assertEqual(jobs, [])
