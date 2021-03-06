# -*- coding: utf-8 -*-
import os
import shutil
import sys
import time

import asyncio
import hangups
import requests

from roboronya.config import (
    IMAGES_DIR, MAX_COMMANDS_PER_MESSAGE,
    MAX_RECONNECT_RETRIES, REFRESH_TOKEN_PATH,
)
from roboronya.exceptions import CommandValidationException
from roboronya.utils import (
    create_path_if_not_exists, dict_update,
    get_file_extension, get_logger, get_uuid
)

import roboronya.plugins as p

logger = get_logger(__name__)


class Roboronya(object):
    """
    The Roboronya bot, most logic here should be base to
    the bot functionality and should not need to be tweaked
    to support more commands / plugins.
    But it probably will...
    """
    def __init__(self):
        self._state = {
            'users': {}
        }
        self._users = {}

    @asyncio.coroutine
    def _on_hangups_connect(self):
        logger.info('Roboronya Connected.')
        self._user_list, self._conv_list = (
            yield from hangups.conversation.build_user_conversation_list(self._hangups)
        )
        self._conv_list.on_event.add_observer(self._on_hangups_event)

    @asyncio.coroutine
    def _on_disconnect(self):
        logger.info('Roboronya Disconnected.')

    @asyncio.coroutine
    def _on_hangups_event(self, conv_event):
        if isinstance(conv_event, hangups.ChatMessageEvent):
            conv = self._conv_list.get(conv_event.conversation_id)
            # Store user reference under generated unique ID.
            for user in conv.users:
                if not user.id_ in self._users:
                    self._store_user_state(user)
            self._handle_message(conv, conv_event)

    def _store_user_state(self, user):
        user_uid = get_uuid()
        self._users[user.id_] = user_uid
        self._state['users'][user_uid] = {
            'user_fullname': user.full_name
        }

    def _process_commands(self, conv, conv_event):
        commands = []
        for token in conv_event.text.split():
            if '/' in token:
                commands.append({
                    'args': [self, conv, []],
                    'name': token.replace('/', '').lower(),
                    'uid': get_uuid()
                })
            else:
                if commands:
                    commands[-1]['args'][-1].append(token)

        # Filter out non-existeng commands.
        return list(filter(lambda c: c['name'] in p.commands, commands))

    def _handle_message(self, conv, conv_event):
        user = conv.get_user(conv_event.user_id)
        # Ignore roboronya's own messages.
        if user.is_self:
            return

        user_uid = self._users[conv_event.user_id]
        kwargs = {
            'log_tag': user_uid,
            'original_message': conv_event.text,
            'user_fullname': (
                self.get_state('users')[user_uid].get('alias') or
                user.full_name
            ),
            'user_uid': user_uid,
        }
        logger.info(
            '[{}] Conversation event received.'.format(user_uid)
        )
        commands = self._process_commands(conv, conv_event)
        if len(commands) > MAX_COMMANDS_PER_MESSAGE:
            logger.info(
                '[{}] Maximum number of commands per message exceeded. '
                'Got: {}. Max: {}.'.format(
                    user_uid,
                    len(commands),
                    MAX_COMMANDS_PER_MESSAGE
                )
            )
            kwargs['max_num_cmds'] = MAX_COMMANDS_PER_MESSAGE
            return self.send_message(
                conv,
                (
                    'Sorry {user_fullname} I can only process '
                    '{max_num_cmds} command(s) per message.'
                ),
                **kwargs
            )

        for command in commands:
            kwargs['command_name'] = command['name']
            kwargs['log_tag'] = '[{}-{}]'.format(
                user_uid, command['uid'],
            )
            try:
                logger.info(
                    '{} Running /{} command with arguments: '
                    '({}).'.format(
                        kwargs['log_tag'],
                        command['name'],
                        ', '.join(command['args'][-1])
                    )
                )
                return p.run(*command['args'], **kwargs)
            except CommandValidationException as e:
                logger.info(
                    '{} Validation error on the command /{}. '
                    'Error: {}'.format(
                        kwargs['log_tag'], command['name'], e
                    )
                )
                return self.send_message(
                    conv, str(e), **kwargs
                )
            except Exception as e:
                logger.info(
                    '{} Something went wrong with the /{} command. '
                    'Error: {}.'.format(
                        kwargs['log_tag'], command['name'], e
                    )
                )
                logger.exception(e)
                return self.send_message(
                    conv,
                    (
                        'Sorry {user_fullname} I failed to process '
                        'your command: "/{command_name}".'
                    ),
                    **kwargs
                )

    def send_message(self, conv, text, **kwargs):
        logger.info(
            '{} Sending response.'.format(kwargs['log_tag'])
        )
        asyncio.async(conv.send_message(
            hangups.ChatMessageSegment.from_str(
                text.format(**kwargs)
            ),
            image_file=kwargs.get('image_file')
        ))

    def send_file(self, conv, text, media_url, **kwargs):
        """
        Send a file to the conversation.
        """
        logger.info(
            '{} Uploading file from url: {}'.format(
                kwargs['log_tag'], media_url,
            )
        )
        response = requests.get(media_url)
        file_path = '{}.{}'.format(
            os.path.join(IMAGES_DIR, get_uuid()),
            get_file_extension(media_url)
        )

        create_path_if_not_exists(file_path)
        with open(file_path, 'wb+') as img:
            img.write(response.content)

        self.send_message(
            conv, text, image_file=open(file_path, 'rb+'), **kwargs
        )

    def get_state(self, name):
        return self._state[name]

    def set_state(self, name, new_state):
        self._state[name] = dict_update(
            self._state[name],
            new_state
        )

    def login(self):
        create_path_if_not_exists(REFRESH_TOKEN_PATH)
        return hangups.auth.get_auth_stdin(
            REFRESH_TOKEN_PATH
        )

    def run(self):
        cookies = self.login()
        if cookies:
            self._loop = asyncio.get_event_loop()
            for retry in range(MAX_RECONNECT_RETRIES):
                try:
                    self._hangups = hangups.Client(cookies)

                    self._hangups.on_connect.add_observer(
                        self._on_hangups_connect
                    )
                    self._hangups.on_disconnect.add_observer(
                        self._on_disconnect
                    )

                    self._loop.run_until_complete(
                        self._hangups.connect()
                    )
                except Exception as e:
                    logger.info(
                        'Roboronya disconnected. '
                        'Retrying {}/{}...'.format(
                            retry + 1,
                            MAX_RECONNECT_RETRIES
                        )
                    )
                    logger.exception(e)
                    time.sleep(5 + retry * 5)

            logger.info('Roboronya is exiting.')
            sys.exit(0)

        logger.info('Invalid login.')
        sys.exit(0)

    def stop(self):
        logger.info('Roboronya was stopped.')
        if os.path.exists(IMAGES_DIR):
            shutil.rmtree(IMAGES_DIR)
        if hasattr(self, '_hangups'):
            asyncio.async(
                self._hangups.disconnect()
            ).add_done_callback(lambda future: future.result())
