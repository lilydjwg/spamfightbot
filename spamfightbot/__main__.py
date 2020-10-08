#!/usr/bin/python3

from __future__ import annotations

import logging
import shelve
import datetime
from typing import Union

import telegram.error
from telegram.ext import Updater
from telegram.ext import MessageHandler, Filters
from telegram.ext import CommandHandler

NEWPAIR_USAGE = '''\
Usage: /newpair @front @group

Users entering @group must be in @front, or get kicked.
You must be an admin of @group and add me as an admin in it.
'''

def format_name(user) -> str:
  l = [user.first_name, user.last_name]
  return ' '.join(x for x in l if x)

class ChatUnavailable(Exception):
  def __init__(self, chat_id: Union[str, int]) -> None:
    self.chat_id = chat_id

class SpamFightBot:
  def __init__(self, store):
    self.store = store

  def newpair(self, update, context):
    msg = update.message
    bot = context.bot
    u = msg.from_user
    logging.debug('newpair msg: %r', msg.text)

    if msg.chat.type in ["group", "supergroup"]:
      msg.delete()
      return

    reply = self.newpair_impl(bot, msg, u)
    u.send_message(
      text = reply,
      reply_to_message_id = msg.message_id,
    )

  def newpair_impl(self, bot, msg, u) -> str:
    try:
      _, front, group = msg.text.split()
    except ValueError:
      return NEWPAIR_USAGE

    try:
      front_g = get_chat_or_fail(bot, front)
      group_g = get_chat_or_fail(bot, group)
    except ChatUnavailable as e:
      return f'Error: the chat {e.chat_id} does not exist or is unavailable to me.'

    admins = bot.get_chat_administrators(group)
    admin_ids = [cm.user.id for cm in admins]
    if u.id not in admin_ids:
      return f'Error: you are not an admin of {group}.'

    if bot.id not in admin_ids:
      return f"Error: I'm not an admin of {group}."

    if front_g.type == 'channel':
      try:
        bot.get_chat_administrators(front)
      except telegram.error.BadRequest: # Member list is inaccessible
        return f"Error: I'm not an admin of {front_g.type} {front} but I need to be in order to see its members."

    self.store[str(group_g.id)] = front_g.id
    return 'Success!'

  def handle_msg(self, update, context):
    msg = update.message
    bot = context.bot

    if msg is None: # edited message
      return

    if msg.left_chat_member:
      if bot.id == msg.left_chat_member.id:
        # I'm removed
        try:
          logging.info('Leaving %s (%d)', msg.chat.title, msg.chat.id)
          del self.store[str(msg.chat.id)]
        except KeyError:
          pass

      elif bot.id == msg.from_user.id:
        # I've removed the user
        bot.delete_message(msg.chat.id, msg.message_id)

    for u in msg.new_chat_members:
      if u.is_bot:
        continue
      logging.info('new user: %s (%d)', format_name(u), u.id)

      group_id = msg.chat.id
      front_id = self.store.get(str(group_id))
      if front_id is None:
        logging.info('Leaving %s (%d)', msg.chat.title, group_id)
        bot.leave_chat(group_id)
        continue

      if msg.from_user.id != u.id:
        logging.info(
          '%s joined by %s',
          format_name(u),
          format_name(msg.from_user),
        )
        continue

      try:
        cm = bot.get_chat_member(front_id, u.id)
        is_member = cm.status in ['member', 'creator', 'administrator']
        logging.debug('ChatMember %r', cm)
      except telegram.error.Unauthorized:
        logging.warning('insuffient permissions for %s for group %s',
                        front_id, msg.chat.title)
        return
      except telegram.error.BadRequest as e:
        logging.warning('get_chat_member error: %r', e)
        is_member = False

      if is_member:
        logging.info('%s joined', format_name(u))
      else:
        logging.info('Removed %s', format_name(u))
        bot.delete_message(msg.chat.id, msg.message_id)
        bot.kick_chat_member(
          msg.chat.id,
          u.id,
          until_date = datetime.datetime.now() + datetime.timedelta(minutes=1),
        )

def get_chat_or_fail(bot, chat_id):
  try:
    return bot.get_chat(chat_id)
  except (telegram.error.BadRequest, telegram.error.Unauthorized):
    raise ChatUnavailable(chat_id)

def main(bot_token, storefile):
  updater = Updater(token=bot_token, use_context=True)
  dispatcher = updater.dispatcher

  store = shelve.open(storefile)
  sfbot = SpamFightBot(store)

  handler = CommandHandler('newpair', sfbot.newpair)
  dispatcher.add_handler(handler)

  handler = MessageHandler(Filters.group, sfbot.handle_msg)
  dispatcher.add_handler(handler)

  updater.start_polling()
  # we can't close store because we ended but not working threads

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

  main(token, args.storefile)
