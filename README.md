# Another Telegram anti-spam bot without disturbance.

Note: this bot is still in beta and bugs may appear.

## The idea

There are quite a few anti-spam bots, but with most (if not all) of them
spammers will still be able to create new messages (e.g. join messages,
verification messages) in the group being protected, either temporarily or
permenantly. This bot aims to not disturb members by getting rid of all these
messages.

The idea is that, for one to successfully join a disscussion group, they must
first join another group or channel (the "gate") according to instructions
(e.g. given in pinned messages). They can leave the gate chat afterwards.
Whoever doesn't follow such instructions will be removed immediately, and their
join messages will be deleted.

The disscussion group remains public so links work, and people can see messages
without joining in.

## How to use the bot

Invite the bot [@spamfightbot](https://t.me/spamfightbot) to a group to be
protected and set it as an administrator (it needs to delete messages and
remove members). To use a channel as the gate the bot must be invited to be an
administrator too so that it can see its members.

Then start the bot and use the command `/newpair @front @group` where `@front`
is to be used as the gate and `@group` is the group to be protected.

Note that the bot may leave by itself if not configured. If you want to disable
the bot for your group, just remove it.

## Run your own instance

Install a recent Python (at least 3.6+) and the `python-telegram-bot` package.
Then you can see its help message:

```
$ python -m spamfightbot --help
usage: __main__.py [-h] [--loglevel {debug,info,warn,error}] [--mail-from ADDRESS] [--mail-errors-to ADDRESS[;ADDRESS]] [FILE]

A Telegram bot to fight spam and keep group chats unbothered

positional arguments:
  FILE                  file path to store some data

optional arguments:
  -h, --help            show this help message and exit
  --loglevel {debug,info,warn,error}
                        log level
  --mail-from ADDRESS   our mail address
  --mail-errors-to ADDRESS[;ADDRESS]
                        mail error logs to ADDRESS via local MTA
```

The bot token is passed by a `TOKEN` environment variable. If you run a local
MTA (e.g. postfix) you can set `--mail-errors-to` to your mail address so that
you see any potential errors (report an issue if you see them and would like to).

The store `FILE` will be created at first run.
