import aiohttp
import asyncio
import fasteners
import functools
import os
import random
from time import time
from urllib.parse import unquote, quote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy

from telethon import TelegramClient
from telethon.errors import *
from telethon.types import InputUser, InputBotAppShortName, InputPeerUser
from telethon.functions import messages, contacts

from .agents import generate_random_user_agent
from bot.config import settings
from typing import Callable
from bot.utils import logger, log_error, proxy_utils, config_utils, CONFIG_PATH, SESSIONS_PATH
from bot.exceptions import InvalidSession
from .headers import headers, get_sec_ch_ua


def speed_calc(referrals_count, time_since_last_boost):
    is_boost = True if time_since_last_boost < 3600 else False
    current_time = int(time())
    days_since_start = (current_time - 1724760000) // 86400
    speed = 0
    if referrals_count >= 300 and days_since_start >= 18:
        speed = 250
    elif referrals_count >= 200 and days_since_start >= 16:
        speed = 200
    elif referrals_count >= 100 and days_since_start >= 14:
        speed = 175
    elif referrals_count >= 50 and days_since_start >= 12:
        speed = 150
    elif referrals_count >= 25 and days_since_start >= 10:
        speed = 125
    elif referrals_count >= 10 and days_since_start >= 8:
        speed = 115
    elif referrals_count >= 5 and days_since_start >= 6:
        speed = 100
    elif referrals_count >= 4 and days_since_start >= 4:
        speed = 50
    elif referrals_count >= 3 and days_since_start >= 2:
        speed = 25
    elif referrals_count >= 1:
        speed = 10

    t = round(1583 + 1583 * speed / 100)

    return t * 2 if is_boost else t


def error_handler(func: Callable):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            await asyncio.sleep(1)
            logger.error(f"{args[0].session_name} | {func.__name__} error: {e}")

    return wrapper


class Tapper:
    def __init__(self, tg_client: TelegramClient):
        self.tg_client = tg_client
        self.session_name, _ = os.path.splitext(os.path.basename(tg_client.session.filename))
        self.config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        self.proxy = self.config.get('proxy', None)
        self.lock = fasteners.InterProcessLock(os.path.join(SESSIONS_PATH, f"{self.session_name}.lock"))
        self.init_data = None
        self.headers = headers
        self.headers['User-Agent'] = self.check_user_agent()
        self.headers.update(**get_sec_ch_ua(self.headers.get('User-Agent', '')))

    def log_message(self, message) -> str:
        return f"<light-yellow>{self.session_name}</light-yellow> | {message}"

    def check_user_agent(self):
        user_agent = self.config.get('user_agent')
        if not user_agent:
            user_agent = generate_random_user_agent()
            self.config['user_agent'] = user_agent
            config_utils.update_session_config_in_file(self.session_name, self.config, CONFIG_PATH)

        return user_agent

    async def get_tg_web_data(self) -> str | None:

        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            proxy_dict = proxy_utils.to_telethon_proxy(proxy)
        else:
            proxy_dict = None
        self.tg_client.set_proxy(proxy_dict)

        init_data = None
        with self.lock:
            async with self.tg_client as client:
                while True:
                    try:
                        resolve_result = await client(contacts.ResolveUsernameRequest(username='HorizonLaunch_bot'))
                        peer = InputPeerUser(user_id=resolve_result.peer.user_id,
                                             access_hash=resolve_result.users[0].access_hash)
                        break
                    except FloodWaitError as fl:
                        fls = fl.seconds

                        logger.warning(self.log_message(f"FloodWait {fl}"))
                        logger.info(self.log_message(f"Sleep {fls}s"))
                        await asyncio.sleep(fls + 3)

                ref_id = settings.REF_ID if random.randint(0, 100) <= 80 else "525256526"

                input_user = InputUser(user_id=resolve_result.peer.user_id, access_hash=resolve_result.users[0].access_hash)
                input_bot_app = InputBotAppShortName(bot_id=input_user, short_name="HorizonLaunch")

                web_view = await self.tg_client(messages.RequestAppWebViewRequest(
                    peer=peer,
                    app=input_bot_app,
                    platform='android',
                    write_allowed=True,
                    start_param=ref_id
                ))

                auth_url = web_view.url
                tg_web_data = unquote(
                    string=unquote(string=auth_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]))
                tg_web_data_parts = tg_web_data.split('&')

                user_data = tg_web_data_parts[0].split('=')[1]
                chat_instance = tg_web_data_parts[1].split('=')[1]
                chat_type = tg_web_data_parts[2].split('=')[1]
                start_param = tg_web_data_parts[3].split('=')[1]
                auth_date = tg_web_data_parts[4].split('=')[1]
                hash_value = tg_web_data_parts[5].split('=')[1]

                user_data_encoded = quote(user_data)

                init_data = (f"user={user_data_encoded}&chat_instance={chat_instance}&chat_type={chat_type}&"
                             f"start_param={start_param}&auth_date={auth_date}&hash={hash_value}")

        return init_data

    @error_handler
    async def make_request(self, http_client, method, endpoint=None, url=None, **kwargs):
        full_url = url or f"https://api.eventhorizongame.xyz{endpoint or ''}"
        response = await http_client.request(method, full_url, **kwargs)
        response.raise_for_status()
        return await response.json()

    @error_handler
    async def login(self, http_client):
        return await self.make_request(http_client, 'POST', endpoint="/auth", json={'auth': self.init_data})

    @error_handler
    async def boost(self, http_client):
        return await self.make_request(http_client, 'POST', endpoint="/tap?boost=true", json={'auth': self.init_data})

    @error_handler
    async def tap_red_button(self, http_client):
        return await self.make_request(http_client, 'POST', endpoint="/tap", json={'auth': self.init_data})

    @error_handler
    async def tap(self, http_client, tap_count):
        return await self.make_request(http_client, 'POST', endpoint=f"/taps?count={tap_count}",
                                       json={'auth': self.init_data})

    async def check_proxy(self, http_client: aiohttp.ClientSession, proxy: str) -> bool:
        try:
            response = await http_client.get(url='https://httpbin.org/ip', timeout=aiohttp.ClientTimeout(10))
            ip = (await response.json()).get('origin')
            logger.info(self.log_message(f"Proxy IP: {ip}"))
            return True
        except Exception as error:
            log_error(self.log_message(f"Proxy: {proxy} | Error: {type(error).__name__}. Skipping session."))
            return False

    async def run(self) -> None:
        if settings.USE_RANDOM_DELAY_IN_RUN:
            random_delay = random.randint(settings.RANDOM_DELAY_IN_RUN[0], settings.RANDOM_DELAY_IN_RUN[1])
            logger.info(self.log_message(f"Bot will start in <m>{random_delay}s</m>"))
            await asyncio.sleep(random_delay)

        proxy_conn = None
        if self.proxy:
            proxy_conn = ProxyConnector().from_url(self.proxy)
            http_client = CloudflareScraper(headers=self.headers, connector=proxy_conn)
            p_type = proxy_conn._proxy_type
            p_host = proxy_conn._proxy_host
            p_port = proxy_conn._proxy_port

            if not await self.check_proxy(http_client=http_client, proxy=f"{p_type}://{p_host}:{p_port}"):
                if not http_client.closed:
                    await http_client.close()
                if proxy_conn and not proxy_conn.closed:
                    proxy_conn.close()
                return

        else:
            http_client = CloudflareScraper(headers=self.headers)

        self.init_data = await self.get_tg_web_data()

        if not self.init_data:
            if not http_client.closed:
                await http_client.close()
            if proxy_conn and not proxy_conn.closed:
                proxy_conn.close()
            return

        while True:
            try:
                if http_client.closed:
                    if proxy_conn and not proxy_conn.closed:
                        proxy_conn.close()

                    proxy_conn = ProxyConnector().from_url(self.proxy) if self.proxy else None
                    http_client = aiohttp.ClientSession(headers=self.headers, connector=proxy_conn)

                info_data = await self.login(http_client=http_client)
                if not info_data or not info_data.get('ok'):
                    logger.info(self.log_message(f"Login failed"))
                    await asyncio.sleep(delay=1800)
                    continue
                await asyncio.sleep(2)

                rocket = info_data.get('rocket', {})
                user_info = info_data.get('user', {})
                logger.info(self.log_message("🚀 Logged in successfully"))

                tap = await self.tap_red_button(http_client=http_client)
                if tap.get("ok", False):
                    rocket = tap.get('rocket', {})
                    user_info = tap.get('user', {})

                boost_attempts = int(rocket.get('boost_attempts', 0))
                current_time = int(time())
                last_boost_timestamp = rocket.get('last_boost_timestamp', 0)
                time_since_last_boost = max(0, current_time - last_boost_timestamp)
                speed = speed_calc(user_info.get('referrals_count', 0), time_since_last_boost)
                logger.info(self.log_message(
                    f"Name: <m>{user_info.get('name')}</m> | Points: <m>{int(rocket.get('distance', 0))}</m> | Speed: <m>{speed}</m>"))
                if user_info.get('referrals_count', 0) >= 1:
                    logger.info(self.log_message(f"You have <m>{6 - boost_attempts}</m> boosts. "
                                f"Next boost in <m>{round((3600 - time_since_last_boost) / 60, 2) if round((3600 - time_since_last_boost) / 60, 2) > 0 else '~'}</m> minutes"))
                    if time_since_last_boost >= 3600 and boost_attempts < 6:
                        boost = await self.boost(http_client=http_client)
                        if boost:
                            rocket = boost.get('rocket', {})
                            last_boost_timestamp = rocket.get('last_boost_timestamp', current_time)
                            time_since_last_boost = 0
                            logger.info(self.log_message(f"<m>Boosted successfully</m>"))
                            await asyncio.sleep(3)

                            if time_since_last_boost < 3600:
                                all_tap_count = int(rocket.get('boost_taps', 0))
                                while all_tap_count < 1000:
                                    remaining_taps = 1000 - all_tap_count
                                    tap_count = min(random.randint(30, 60), remaining_taps)
                                    all_tap_count += tap_count

                                    taps = await self.tap(http_client=http_client, tap_count=tap_count)
                                    if taps:
                                        rocket = taps.get('rocket', {})
                                        logger.info(self.log_message(f"Tapped <m>{all_tap_count} / 1000</m> | "
                                                    f"Distance: <m>{int(rocket.get('distance', 0))}</m>"))
                                        sleep_time = random.randint(1, 3)
                                        await asyncio.sleep(sleep_time)

                            sleep_time = 3600 - time_since_last_boost
                        else:
                            sleep_time = 3600
                    else:
                        sleep_time = 3600
                else:
                    sleep_time = 3600

                logger.info(self.log_message(f"Sleep <m>{sleep_time}s</m>"))

                await http_client.close()
                if proxy_conn:
                    if not proxy_conn.closed:
                        proxy_conn.close()
                await asyncio.sleep(delay=sleep_time)
            except InvalidSession as error:
                raise error

            except Exception as error:
                log_error(self.log_message(f"Unknown error: {error}"))
                await asyncio.sleep(delay=3)
                logger.info(self.log_message(f'Sleep <m>600s</m>'))
                await asyncio.sleep(600)


async def run_tapper(tg_client: TelegramClient):
    runner = Tapper(tg_client=tg_client)
    try:
        await runner.run()
    except InvalidSession as e:
        logger.error(runner.log_message(f"Invalid Session: {e}"))
    finally:
        if runner.lock.acquired:
            runner.lock.release()
