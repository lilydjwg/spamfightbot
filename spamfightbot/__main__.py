#!/usr/bin/python3

from __future__ import annotations

import logging
import shelve
import time
from typing import Union
import asyncio

from aiogram import Bot, Dispatcher, types
from aiogram.utils import exceptions

from .lib.expiringdict import ExpiringDict

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
    dp = Dispatcher(bot)

    dp.register_message_handler(
      self.newpair,
      commands=['newpair'],
    )

    dp.register_message_handler(
      self.on_message,
      content_types = types.ContentTypes.ANY,
    )

    self.dp = dp
    self.bot = bot

  async def newpair(self, msg):
    bot = self.bot
    u = msg.from_user
    logging.debug('newpair msg: %r', msg.text)

    if msg.chat.type in ["group", "supergroup"]:
      try:
        await msg.delete()
      except exceptions.MessageCantBeDeleted:
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
      except exceptions.BadRequest: # Member list is inaccessible
        return f"Error: I'm not an admin of {front_g.type} {front} but I need to be in order to see its members."

    self.store['front_groups'] = {g for g in self.store.values() if isinstance(g, int)}
    self.store[str(group_g.id)] = front_g.id
    logging.info('new pair: %s and %s', front, group)
    return 'Success!'

  async def on_message(self, msg: types.Message) -> None:
    try:
      await self._on_message_real(msg)
    except (exceptions.MessageCantBeDeleted, exceptions.NotEnoughRightsToRestrict):
      logging.info('Leaving %s (%d) (message can\'t be deleted)',
                   msg.chat.title, msg.chat.id)
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
      logging.info('Missed message, deleting: %s', msg.text)
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
          logging.info('Leaving %s (%d) (self removed)', msg.chat.title, msg.chat.id)
          del self.store[str(msg.chat.id)]
        except KeyError:
          pass

      elif self.bot_id == msg.from_user.id:
        # I've removed the user
        await bot.delete_message(msg.chat.id, msg.message_id)

    for u in msg.new_chat_members:
      if u.is_bot:
        continue
      logging.info('new user: %s (%d)', u.full_name, u.id)

      group_id = msg.chat.id
      front_id = self.store.get(str(group_id))
      if front_id is None:
        if group_id not in self.store['front_groups']:
          # leave any unconfigured groups
          logging.info('Leaving %s (%d) (unconfigured)', msg.chat.title, group_id)
          await bot.leave_chat(group_id)
        continue

      if msg.from_user.id != u.id:
        logging.info(
          '%s added by %s',
          u.full_name,
          msg.from_user.full_name,
        )
        cm = await bot.get_chat_member(group_id, msg.from_user.id)
        is_member = cm.status in ['member', 'creator', 'administrator']
      else:
        self.newuser_msgs[key] = []
        try:
          cm = await bot.get_chat_member(front_id, u.id)
          is_member = cm.status in ['member', 'creator', 'administrator']
          logging.debug('ChatMember %r', cm)
        except exceptions.Unauthorized:
          logging.warning('insuffient permissions for %s for group %s',
                          front_id, msg.chat.title)
          return
        except exceptions.BadRequest as e:
          # may be ChatNotFound
          logging.warning('get_chat_member error: %r', e)
          # error treated as open
          is_member = True

      if is_member:
        logging.info('%s joined', u.full_name)
        try:
          del newuser_msgs[key]
        except KeyError:
          pass
      else:
        logging.info('Removing %s', u.full_name)
        self.just_banned[key] = True
        await bot.kick_chat_member(
          msg.chat.id,
          u.id,
          # python-telegram-bot has changed timezone handling silently,
          # causing blocking people forever
          # I've switched to aiogram, but I don't want to be bitten again.
          until_date = int(time.time() + 60),
        )
        try:
          await bot.delete_message(msg.chat.id, msg.message_id)
        except exceptions.MessageToDeleteNotFound:
          # message deleted by others
          pass

        # delete received spam message
        if msgs := newuser_msgs.pop(key, None):
          logging.info(
            'Removing %d messages(s) from %s',
            len(msgs), u.full_name
          )
          for msg_id in msgs:
            await bot.delete_message(msg.chat.id, msg_id)

  async def run(self) -> None:
    self.bot_id = (await self.bot.me).id
    await self.dp.skip_updates()
    await self.dp.start_polling()

async def get_chat_or_fail(bot: Bot, chat_id: Union[int, str]) -> types.Chat:
  try:
    return await bot.get_chat(chat_id)
  except (exceptions.BadRequest, exceptions.Unauthorized):
    raise ChatUnavailable(chat_id)

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

  if args.mail_errors_to:
    rootlogger = logging.getLogger()
    handler = LocalSMTPHandler(
      args.mail_from,
      args.mail_errors_to.split(';'),
      tag = 'spamfightbot',
      min_gap_seconds = 3600,
    )
    handler.setLevel(logging.WARNING)
    handler.setFormatter(TornadoLogFormatter(color=False))
    rootlogger.addHandler(handler)

  try:
    asyncio.run(main(token, args.storefile))
  except KeyboardInterrupt:
    pass
