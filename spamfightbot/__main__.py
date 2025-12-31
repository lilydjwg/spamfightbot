#!/usr/bin/python3

from __future__ import annotations

import logging
import shelve
import time
from typing import Union
import asyncio
import json

from aiogram import Bot, Dispatcher, types, loggers
from aiogram import exceptions
from aiogram.filters.command import Command
from aiogram.utils.serialization import deserialize_telegram_object_to_python

from .lib.expiringdict import ExpiringDict

logger = logging.getLogger(__name__)

NEWPAIR_USAGE = '''\
Usage: /newpair @front @group

Users entering @group must be in @front, or get kicked.
You must be an admin of @group and add me as an admin in it.
'''

class ChatUnavailable(Exception):
  def __init__(self, chat_id: Union[str, int]) -> None:
    self.chat_id = chat_id

class SpamFightBot:
  def __init__(self, store, token):
    self.store = store
    store['front_groups'] = {g for g in store.values() if isinstance(g, int)}
    self.newuser_msgs = ExpiringDict(300, maxsize=100)
    # we banned a member for 60s so in 50s whatever we receive is missed
    # and shoud be deleted
    self.just_banned = ExpiringDict(50, maxsize=100)

    bot = Bot(token=token)
    dp = Dispatcher()

    dp.message(Command('newpair'))(self.newpair)
    dp.message(lambda event: True)(self.on_message)

    self.dp = dp
    self.bot = bot

  async def newpair(self, msg):
    bot = self.bot
    u = msg.from_user
    logger.debug('newpair msg: %r', msg.text)

    if msg.chat.type in ["group", "supergroup"]:
      try:
        await msg.delete()
      except exceptions.TelegramAPIError:
        pass
      return

    reply = await self.newpair_impl(bot, msg, u)
    await bot.send_message(
      u.id,
      text = reply,
      reply_to_message_id = msg.message_id,
    )

  async def newpair_impl(self, bot, msg, u) -> str:
    try:
      _, front, group = msg.text.split()
    except ValueError:
      return NEWPAIR_USAGE

    try:
      front_g = await get_chat_or_fail(bot, front)
      group_g = await get_chat_or_fail(bot, group)
    except ChatUnavailable as e:
      return f'Error: the chat {e.chat_id} does not exist or is unavailable to me.'

    if group_g.type not in ['group', 'supergroup']:
      return f'Error: {group} is not a group.'

    admins = await bot.get_chat_administrators(group)
    admin_ids = [cm.user.id for cm in admins]
    if u.id not in admin_ids:
      return f'Error: you are not an admin of {group}.'

    if bot.id not in admin_ids:
      return f"Error: I'm not an admin of {group}."

    if front_g.type == 'channel':
      try:
        await bot.get_chat_administrators(front)
      except exceptions.TelegramBadRequest: # Member list is inaccessible
        return f"Error: I'm not an admin of {front_g.type} {front} but I need to be in order to see its members."

    self.store['front_groups'] = {g for g in self.store.values() if isinstance(g, int)}
    self.store[str(group_g.id)] = front_g.id
    logger.info('new pair: %s and %s', front, group)
    return 'Success!'

  async def on_message(self, msg: types.Message) -> None:
    try:
      if logger.isEnabledFor(logging.DEBUG):
        msg_str = json.dumps(deserialize_telegram_object_to_python(msg), ensure_ascii=False)
        logger.debug('Message: %s', msg_str)
      await self._on_message_real(msg)
    except exceptions.TelegramNetworkError as e:
      logger.warning('TelegramNetworkError: %r', e)
    except exceptions.TelegramAPIError as e:
      if 'not found' in repr(e):
        # deleted by other users
        return

      logger.info('Leaving %s (%d) (%r)', msg.chat.title, msg.chat.id, e)
      await self.bot.leave_chat(msg.chat.id)
      try:
        del self.store[str(msg.chat.id)]
      except KeyError:
        pass

  async def _on_message_real(self, msg: types.Message) -> None:
    bot = self.bot

    self.just_banned.expire()
    key = msg.from_user.id, msg.chat.id
    if key in self.just_banned:
      logger.info('Missed message, deleting: %s', msg.text)
      await bot.delete_message(msg.chat.id, msg.message_id)
      return

    newuser_msgs = self.newuser_msgs
    newuser_msgs.expire()

    if (known_msgs := newuser_msgs.get(key)) is not None:
      # save for later deletion if not passed
      known_msgs.append(msg.message_id)

    if msg.left_chat_member:
      if self.bot_id == msg.left_chat_member.id:
        # I'm removed
        try:
          logger.info('Leaving %s (%d) (self removed)', msg.chat.title, msg.chat.id)
          del self.store[str(msg.chat.id)]
        except KeyError:
          pass

      elif self.bot_id == msg.from_user.id:
        # I've removed the user
        await bot.delete_message(msg.chat.id, msg.message_id)

    if not msg.new_chat_members:
      return

    for u in msg.new_chat_members:
      if u.is_bot:
        continue
      logger.info('new user: %s (%d) in %s', u.full_name, u.id, msg.chat.title)

      group_id = msg.chat.id
      front_id = self.store.get(str(group_id))
      if front_id is None:
        if group_id not in self.store['front_groups']:
          # leave any unconfigured groups
          logger.info('Leaving %s (%d) (unconfigured)', msg.chat.title, group_id)
          await bot.leave_chat(group_id)
        continue

      if msg.from_user.id != u.id:
        logger.info(
          '%s added to %s by %s',
          u.full_name,
          msg.chat.title,
          msg.from_user.full_name,
        )
        cm = await bot.get_chat_member(group_id, msg.from_user.id)
        is_member = cm.status in ['member', 'creator', 'administrator']
      else:
        self.newuser_msgs[key] = []
        try:
          cm = await get_chat_member_retrying(bot, front_id, u.id)
          is_member = cm.status in ['member', 'creator', 'administrator']
          logger.debug('ChatMember %r', cm)
        except exceptions.TelegramForbiddenError:
          logger.warning('insuffient permissions for %s for group %s',
                          front_id, msg.chat.title)
          return
        except exceptions.TelegramAPIError as e:
          # may be chat not found
          logger.error('get_chat_member error: %r', e)
          # error treated as open
          is_member = True

      if is_member:
        logger.info('%s joined %s', u.full_name, msg.chat.title)
        try:
          del newuser_msgs[key]
        except KeyError:
          pass
      else:
        logger.info('Removing %s', u.full_name)
        self.just_banned[key] = True
        await bot.ban_chat_member(
          msg.chat.id,
          u.id,
          # python-telegram-bot has changed timezone handling silently,
          # causing blocking people forever
          # I've switched to aiogram, but I don't want to be bitten again.
          until_date = int(time.time() + 60),
        )
        try:
          await bot.delete_message(msg.chat.id, msg.message_id)
        except exceptions.TelegramNotFound:
          # message deleted by others
          pass

        # delete received spam message
        if msgs := newuser_msgs.pop(key, None):
          logger.info(
            'Removing %d messages(s) from %s',
            len(msgs), u.full_name
          )
          for msg_id in msgs:
            await bot.delete_message(msg.chat.id, msg_id)

  async def run(self) -> None:
    self.bot_id = (await self.bot.me()).id
    await self.bot.delete_webhook(drop_pending_updates=True)
    await self.dp.start_polling(self.bot)

async def get_chat_or_fail(bot: Bot, chat_id: Union[int, str]) -> types.Chat:
  try:
    return await bot.get_chat(chat_id)
  except exceptions.TelegramAPIError:
    raise ChatUnavailable(chat_id)

async def get_chat_member_retrying(bot: Bot, chat_id: int, uid: int) -> types.ChatMember:
  for i in range(3):
    try:
      return await bot.get_chat_member(chat_id, uid)
    except exceptions.TelegramNetworkError as e:
      if i == 2:
        logger.error('get_chat_member error, giving up: %r', e)
        raise
      else:
        logger.warning('get_chat_member error, retrying: %r', e)

async def main(bot_token, storefile):
  with shelve.open(storefile) as store:
    sfbot = SpamFightBot(store, bot_token)
    await sfbot.run()

if __name__ == '__main__':
  import os, sys
  import argparse
  from .lib.nicelogger import enable_pretty_logging, TornadoLogFormatter
  from .lib.mailerrorlog import LocalSMTPHandler

  parser = argparse.ArgumentParser(
    description='A Telegram bot to fight spam and keep group chats unbothered')
  parser.add_argument('storefile', metavar='FILE',
                      nargs='?', default='spamfightbot.store',
                      help='file path to store some data')
  parser.add_argument('--loglevel', default='info',
                      choices=['debug', 'info', 'warn', 'error'],
                      help='log level')
  parser.add_argument('--mail-from', metavar='ADDRESS', default='spamfightbot',
                      help='our mail address')
  parser.add_argument('--mail-errors-to', metavar='ADDRESS[;ADDRESS]',
                      help='mail error logs to ADDRESS via local MTA')
  args = parser.parse_args()

  token = os.environ.pop('TOKEN', None)
  if not token:
    sys.exit('Please pass bot token in environment variable TOKEN.')

  enable_pretty_logging(args.loglevel.upper())

  # don't output "Update id=... is handled. Duration 16 ms by bot id=..." messages
  loggers.event.setLevel(logging.WARNING)

  if args.mail_errors_to:
    rootlogger = logging.getLogger()
    handler = LocalSMTPHandler(
      args.mail_from,
      args.mail_errors_to.split(';'),
      tag = 'spamfightbot',
      delay = 600,
    )
    handler.setLevel(logging.WARNING)
    handler.setFormatter(TornadoLogFormatter(color=False))
    rootlogger.addHandler(handler)

  try:
    asyncio.run(main(token, args.storefile))
  except KeyboardInterrupt:
    pass
