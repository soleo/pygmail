import re
import string
import message as GM
from pygmail.utilities import extract_data, _cmd_cb, _cmd, _cmd_in, _log
import pygmail.errors

GM_ID_EXTRACTOR = re.compile(r'\d+ \(X-GM-MSGID (\d+)\)')

uid_fields = 'X-GM-MSGID UID'
meta_fields = 'INTERNALDATE X-GM-MSGID X-GM-LABELS UID FLAGS'
header_fields = 'BODY.PEEK[HEADER]'
body_fields = 'BODY.PEEK[]'
teaser_fields = 'BODY.PEEK[1]'

imap_queries = dict(
    gm_id='(X-GM-MSGID)',
    uid='({uid})'.format(uid=uid_fields),
    body='({meta} {body})'.format(meta=meta_fields, body=body_fields),
    teaser='({meta} BODYSTRUCTURE {header} {teaser})'.format(meta=meta_fields,
                                                             header=header_fields,
                                                             teaser=teaser_fields),
    header='({meta} {header})'.format(meta=meta_fields, header=header_fields)
)

METADATA = 0
HEADERS = 1
BODY = 2


def parse_fetch_request(response, mailbox, teaser=False, full=False, gm_id=False):

    messages = []

    # Quickly we can search for the simplest case, where we have no
    # message parts to return
    if not response or not response[0]:
        return messages

    message_complete = False

    if gm_id:
        for part in response:
            gm_id_match = GM_ID_EXTRACTOR.match(part)
            if gm_id_match:
                messages.append(gm_id_match.group(1))
    elif teaser:
        end_metadata = False
        end_header = False
        metadata_section = ''
        header_section = ''
        body_section = ''
        for part in response:
            if isinstance(part, basestring):
                message_complete = True
            else:
                for sub_part in part:
                    if not end_metadata:
                        metadata_section += sub_part
                        if 'BODY[HEADER]' in sub_part:
                            end_metadata = True
                    elif not end_header:
                        if 'BODY[1]' in sub_part:
                            end_header = True
                        else:
                            header_section += sub_part
                        if 'BODY[1] NIL)' in sub_part:
                            message_complete = True
                    else:
                        body_section += sub_part
            if message_complete:
                messages.append(GM.MessageTeaser(mailbox,
                                                 metadata=metadata_section,
                                                 headers=header_section,
                                                 body=body_section))
                message_complete = False
                end_metadata = False
                end_header = False
                metadata_section = ''
                header_section = ''
                body_section = ''
    # Full messages just come in two tuples, the first being the full message
    # test, and the second a terminator
    elif full:
        message_parts = []
        for part in response:
            # The first thing we expect to see when iterating over parts of
            # a fill message is a single tuple, with two parts in it (ie
            # a nested tuple).  The first part will be the metadata, the second
            # will be both the headers and the body (since the message class
            # parses both from the same contents)
            if len(message_parts) == 0:
                message_parts.append(part[0])
                message_parts.append(part[1])
                message_parts.append(part[1])
            # Before we complete the message though, we need to read off the
            # terminating section of the complete message. The below check
            # has the effect of ignoring every part of a set of full messages
            elif len(message_parts) == 3:
                message_complete = True

            if message_complete:
                messages.append(GM.Message(mailbox,
                                           metadata=message_parts[METADATA],
                                           headers=message_parts[HEADERS],
                                           body=message_parts[BODY]))
                message_parts[:] = []
                message_complete = False
    # The remaining option is that we're only reading headers from the mailbox
    # in this case, we also expect pairs of values, the first being a nested
    # tuple of headers and metadata (ie [(metadata, headers)]), and following
    # that a terminating paren character
    else:
        message_parts = []
        for part in response:
            # If we don't currently have any message parts capture,
            # we expect the next chunk to be a tuple, which is nested and
            # contains two subparts, the metadata and the headers
            if len(message_parts) == 0:
                message_parts.append(part[0])
                message_parts.append(part[1])
            # Otherwise, we expect to see a terminator character, which we
            # ignore / don't store, and complete the message
            elif len(message_parts) == 2:
                message_complete = True

            if message_complete:
                messages.append(GM.MessageHeaders(mailbox,
                                                  metadata=message_parts[METADATA],
                                                  headers=message_parts[HEADERS]))
                message_parts[:] = []
                message_complete = False
    return messages


def page_from_list(a_list, limit, offset):
    """ Retreives the paginated section from the provided list

    Helps pagination needs by extracting only the section of the given list
    described by the limit and offset parameters w/o causing invalid index
    errors.

    Args:
        a_list -- A list of any length
        limit  -- The maximum number of elements to return from the list.
                  If False, no items will be truncated
        offset -- The index of the first element in the list to return

    Return:
        A slice from the given list with at most "limit" elements, or all
        elements after offset if limit is False

    """
    count = len(a_list)
    # If the given offset is greater than the total number
    # of messages in the inbox, there are no messages to return
    if count <= offset:
        return []
    else:
        first_elm_index = offset

    if limit is False:
        return a_list[first_elm_index:]
    else:
        last_req_item = offset + limit
        last_elm_index = count if last_req_item >= count else last_req_item
        return a_list[first_elm_index:last_elm_index]


class Mailbox(object):
    """Represents a single mailbox within a gmail account

    Instances of this class are not intended to be initilized directly, but
    instead managed by a pygmail.account.Account instances

    """

    # Classwide regular expression used to extract the human readable versions
    # of the mailbox names from the full, IMAP versions
    NAME_PATTERN = re.compile(r'\((.*?)\) "(.*)" (.*)')

    # Classwide, simple regular expression to only digits in a string
    COUNT_PATTERN = re.compile(r'[^0-9]')

    def __init__(self, account, full_name):
        """ Initilizes a mailbox object

        Args:
            account      -- An initilized pygmail.account.Account object, which
                            represents the gmail account this mailbox exists in
            mailbox_name -- The full name of the mailbox, in IMAP format, not
                            in easy, human readable format

        """
        self.account = account
        self.conn = account.connection
        self.full_name = full_name
        self.name = Mailbox.NAME_PATTERN.match(full_name).groups()[2]

    def __str__(self):
        return "<Mailbox: %s>" % (self.name,)

    def count(self, callback=None):
        """Returns a count of the number of emails in the mailbox

        Returns:
            The int value of the number of emails in the mailbox, or None on
            error

        """
        @pygmail.errors.check_imap_response(callback)
        def _on_select_complete(imap_response):
            data = extract_data(imap_response)
            self.account.last_viewed_mailbox = self
            msg_count = int(Mailbox.COUNT_PATTERN.sub("", str(data)))
            return _cmd(callback, msg_count)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            return _cmd_cb(connection.select, _on_select_complete,
                           bool(callback), self.name)

        return _cmd_cb(self.account.connection, _on_connection, bool(callback))

    def delete_message(self, uid, message_id, trash_folder, callback=None):
        """Allows for deleting a message by UID, without needing to pulldown
        and populate a Message object first.

        Args:
            uid          -- the uid for a message in the current mailbox
            message_id   -- the message id, from the email headers of the
                            message to delete
            trash_folder -- the name of the folder / label that is, in the
                            current account, the trash container

        Returns:
            A boolean description of whether a message was successfully deleted
        """
        @pygmail.errors.check_imap_response(callback)
        def _on_original_mailbox_reselected(imap_response):
            return _cmd(callback, True)

        @pygmail.errors.check_imap_state(callback)
        def _on_recevieved_connection_7(connection):
            return _cmd_cb(connection.select, _on_original_mailbox_reselected,
                           bool(callback))

        @pygmail.errors.check_imap_response(callback)
        def _on_expunge_complete(imap_response):
            return _cmd_cb(self.conn, _on_recevieved_connection_7, bool(callback))

        @pygmail.errors.check_imap_state(callback)
        def _on_recevieved_connection_6(connection):
            return _cmd_cb(connection.expunge, _on_expunge_complete, bool(callback))

        @pygmail.errors.check_imap_response(callback)
        def _on_delete_complete(imap_response):
            return _cmd_cb(self.conn, _on_recevieved_connection_6, bool(callback))

        @pygmail.errors.check_imap_state(callback)
        def _on_received_connection_4(connection, deleted_uid):
            del self.num_tries
            return _cmd_cb(connection.uid, _on_delete_complete, bool(callback),
                            'STORE', deleted_uid, 'FLAGS', '\\Deleted')

        @pygmail.errors.check_imap_response(callback)
        def _on_search_for_message_complete(imap_response):
            data = extract_data(imap_response)

            # Its possible here that we've tried to select the message
            # we want to delete from the trash bin before google has
            # registered it there for us.  If our search attempt returned
            # a uid, then we're good to go and can continue.
            try:
                deleted_uid = data[0].split()[-1]
                cbp = dict(deleted_uid=deleted_uid)
                return _cmd_cb(self.conn, _on_received_connection_4,
                               bool(callback), callback_args=cbp)

            # If not though, we should wait a couple of seconds and try
            # again.  We'll do this a maximum of 5 times.  If we still
            # haven't had any luck at this point, we give up and return
            # False, indiciating we weren't able to delete the message
            # fully.
            except IndexError:
                self.num_tries += 1

                # If this is the 5th time we're trying to delete this
                # message, we're going to call it a loss and stop trying.
                # We do some minimal clean up and then just bail out
                # Otherwise, schedule another attempt in 2 seconds and
                # hope that gmail has updated its indexes by then
                if self.num_tries == 5:
                    del self.num_tries
                    if __debug__:
                        _log.error("Giving up trying to delete message")
                        _log.error("got response: {response}".format(response=str(imap_response)))
                    return _cmd(callback, False)
                else:
                    if __debug__:
                        _log.error("Try {num} to delete deleting message.  Waiting".format(num=self.num_tries))
                        _log.error("got response: {response}".format(response=str(imap_response)))
                    return _cmd_in(_on_trash_selected, 2, bool(callback))

        @pygmail.errors.check_imap_state(callback)
        def _on_received_connection_3(connection):
            return _cmd_cb(connection.uid, _on_search_for_message_complete,
                           bool(callback), 'search', None, 'X-GM-RAW',
                            '"rfc822msgid:{msg_id}"'.format(msg_id=message_id))

        @pygmail.errors.check_imap_response(callback)
        def _on_trash_selected(imap_response):
            # It can take several attempts for the deleted message to show up
            # in the trash label / folder.  We'll try 5 times, waiting
            # two sec between each attempt
            return _cmd_cb(self.conn, _on_received_connection_3, bool(callback))

        @pygmail.errors.check_imap_state(callback)
        def _on_received_connection_2(connection):
            self.num_tries = 0
            return _cmd_cb(connection.select, _on_trash_selected, bool(callback))

        @pygmail.errors.check_imap_response(callback)
        def _on_message_moved(imap_response):
            return _cmd_cb(self.conn, _on_received_connection_2, bool(callback))

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            return _cmd_cb(connection.uid, _on_message_moved, bool(callback),
                            'COPY', uid, trash_folder)

        @pygmail.errors.check_imap_response(callback)
        def _on_select(was_selected):
            # self.account.connection(callback=_on_connection)
            return _cmd_cb(self.account.connection, _on_connection,
                           bool(callback))

        return _cmd_cb(self.select, _on_select, bool(callback))

    def delete(self, callback=None):
        """Removes the mailbox / folder from the current gmail account. In
        Gmail's implementation, this translates into deleting a Gmail label.

        Return:
            True if a folder / label was removed. Otherwise, False (such
            as if the current folder / label doesn't exist at deletion)
        """
        @pygmail.errors.check_imap_response(callback, require_ok=False)
        def _on_mailbox_deletion(imap_response):
            data = extract_data(imap_response)
            was_success = data[0] == "Success"
            return _cmd(callback, was_success)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            if pygmail.errors.is_auth_error(connection):
                return _cmd(callback, connection)
            else:
                return _cmd_cb(connection.delete, _on_mailbox_deletion,
                               bool(callback), self.name)

        return _cmd_cb(self.account.connection, _on_connection, bool(callback))

    def select(self, callback=None):
        """Sets this mailbox as the current active one on the IMAP connection

        In order to make sure we don't make many many redundant calls to the
        IMAP server, we allow the account managing object to keep track
        of which mailbox was last set as active.  If the current mailbox is
        active, this method does nothing.

        Returns:
            True if any changes were made, otherwise False

        """
        def _on_count_complete(num):
            self.account.last_viewed_mailbox = self
            return _cmd(callback, True)

        if self is self.account.last_viewed_mailbox:
            return _cmd(callback, False)
        else:
            return _cmd_cb(self.count, _on_count_complete, bool(callback))

    def search(self, term, limit=100, offset=0, only_uids=False,
               full=False, callback=None, **kwargs):
        """Searches for messages in the inbox that contain a given phrase

        Seaches for a given phrase in the current mailbox, and returns a list
        of messages that have the phrase in the HTML and/or plain text part
        of their body.

        Note that this search is done on the server, and not against the
        message text directly, so its not a string level search (it falls
        through to Google's more intellegent search)

        Args:
            term -- the search term to search for in the current mailbox

        Keyword arguments:
            limit        -- The maximum number of messages to return
            offset       -- The first message to return out of the entire set of
                            messages in the inbox
            gm_ids       -- If True, only the unique, persistant X-GM-MSGID
                            value for the email message will be returned
            only_uids    -- If True, only the UIDs of the matching messages will
                            be returned, instead of full message headers.
            full         -- Whether to fetch the entire message, instead of
                            just the headers.  Note that if only_uids is True,
                            this parameter will have no effect.
            teaser       -- Whether to fetch just a brief, teaser version of the
                            body (ie the first mime section).  Note that this
                            option is incompatible with the full
                            option, and the former will take precedence


        Returns:
            A list of messages or uids (depending on the call arguments) in case
            of success, and an IMAPError object in all other cases.
        """
        teasers = kwargs.get("teaser")
        gm_ids = kwargs.get('gm_ids')

        def _on_messages_by_id(messages):
            return _cmd(callback, messages)

        @pygmail.errors.check_imap_response(callback)
        def _on_search(imap_response):
            data = extract_data(imap_response)
            ids = string.split(data[0])
            ids_to_fetch = page_from_list(ids, limit, offset)
            return _cmd_cb(self.messages_by_id, _on_messages_by_id,
                           bool(callback), ids_to_fetch, only_uids=only_uids,
                           full=full, teaser=teasers, gm_ids=gm_ids)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            return _cmd_cb(connection.search, _on_search, bool(callback),
                           None, 'X-GM-RAW', term)

        @pygmail.errors.check_imap_response(callback)
        def _on_mailbox_selected(was_changed):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        return _cmd_cb(self.select, _on_mailbox_selected, bool(callback))

    def messages(self, limit=100, offset=0, callback=None, **kwargs):
        """Returns a list of all the messages in the inbox

        Fetches a list of all messages in the inbox.  This list is by default
        limited to only the first 100 results, though pagination can trivially
        be implemented using the limit / offset parameters

        Keyword arguments:
            limit     -- The maximum number of messages to return.  If None,
                         everything will be returned
            offset    -- The first message to return out of the entire set of
                         messages in the inbox
            gm_ids    -- If True, only the unique, persistant X-GM-MSGID
                         value for the email message will be returned
            only_uids -- If True, only the UIDs of the matching messages will
                         be returned, instead of full message headers.
            full      -- Whether to fetch the entire message, instead of
                         just the headers.  Note that if only_uids is True,
                         this parameter will have no effect.
            teaser    -- Whether to fetch just a brief, teaser version of the
                         body (ie the first mime section).  Note that this
                         option is incompatible with the full
                         option, and the former will take precedence

        Return:

            A two index tupple.  The element in the first index is a
            list of zero or more pygmail.message.Message objects (or uids if
            only_uids is TRUE), or None if no information could be found about
            the mailbox. The second element is the total number of messages (not
            just those returned from the limit-offset parameters)

        """
        teasers = kwargs.get('teaser')
        full = kwargs.get('full')
        only_uids = kwargs.get('only_uids')
        gm_ids = kwargs.get('gm_ids')

        def _on_messages_by_id(messages):
            return _cmd(callback, messages)

        @pygmail.errors.check_imap_response(callback)
        def _on_search(imap_response):
            data = extract_data(imap_response)
            ids = string.split(data[0])
            ids_to_fetch = page_from_list(ids, limit, offset)
            return _cmd_cb(self.messages_by_id, _on_messages_by_id,
                           bool(callback), ids_to_fetch, only_uids=only_uids,
                           full=full, teaser=teasers, gm_ids=gm_ids)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            return _cmd_cb(connection.search, _on_search, bool(callback), None, 'ALL')

        @pygmail.errors.check_imap_response(callback)
        def _on_select_complete(result):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        return _cmd_cb(self.select, _on_select_complete, bool(callback))

    def fetch_all(self, uids, full=False, callback=None, **kwargs):
        """Returns a list of messages, each specified by their UID

        Returns zero or more GmailMessage objects, each representing a email
        message in the current mailbox.

        Arguments:
            uids -- A list of zero or more email uids

        Keyword Args:
            gm_ids  -- If True, only the unique, persistant X-GM-MSGID
                       value for the email message will be returned
            full    -- Whether to fetch the entire message, instead of
                       just the headers.  Note that if only_uids is True,
                       this parameter will have no effect.
            teaser  -- Whether to fetch just a brief, teaser version of the
                       body (ie the first mime section).  Note that this
                       option is incompatible with the full
                       option, and the former will take precedence

        Returns:
            Zero or more pygmail.message.Message objects, representing any
            messages that matched a provided uid
        """
        teasers = kwargs.get("teaser")
        gm_ids = kwargs.get('gm_ids')

        @pygmail.errors.check_imap_response(callback)
        def _on_fetch(imap_response):
            data = extract_data(imap_response)
            messages = parse_fetch_request(data, self, teasers, full, gm_ids)
            return _cmd(callback, messages)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            if gm_ids:
                request = imap_queries["gm_id"]
            elif full:
                request = imap_queries["body"]
            elif teasers:
                request = imap_queries["teaser"]
            else:
                request = imap_queries["header"]
            return _cmd_cb(connection.uid, _on_fetch, bool(callback),
                           "FETCH", ",".join(uids), request)

        def _on_select(result):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        if uids:
            return _cmd_cb(self.select, _on_select, bool(callback))
        else:
            return _cmd(callback, None)

    def fetch(self, uid, full=False, callback=None, **kwargs):
        """Returns a single message from the mailbox by UID

        Returns a single message object, representing the message in the current
        mailbox with the specific UID

        Arguments:
            uid -- the numeric, unique identifier of the message in the mailbox

        Keyword Args:
            gm_ids  -- If True, only the unique, persistant X-GM-MSGID
                       value for the email message will be returned
            full    -- Whether to fetch the entire message, instead of
                       just the headers.  Note that if only_uids is True,
                       this parameter will have no effect.
            teaser  -- Whether to fetch just a brief, teaser version of the
                       body (ie the first mime section).  Note that this
                       option is incompatible with the full
                       option, and the former will take precedence

        Returns:
            A pygmail.message.Message object representing the email message, or
            None if none could be found.  If an error is encountered, an
            IMAPError object will be returned.
        """
        teasers = kwargs.get("teaser")
        gm_ids = kwargs.get('gm_ids')

        @pygmail.errors.check_imap_response(callback)
        def _on_fetch(imap_response):
            data = extract_data(imap_response)
            messages = parse_fetch_request(data, self, teasers, full, gm_ids)
            return _cmd(callback, messages[0] if len(messages) > 0 else None)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            if gm_ids:
                request = imap_queries["gm_id"]
            elif full:
                request = imap_queries["body"]
            elif teasers:
                request = imap_queries["teaser"]
            else:
                request = imap_queries["header"]
            return _cmd_cb(connection.uid, _on_fetch, bool(callback),
                           "FETCH", uid, request)

        @pygmail.errors.check_imap_response(callback)
        def _on_select(result):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        return _cmd_cb(self.select, _on_select, bool(callback))

    def fetch_gm_id(self, gm_id, full=False, callback=None, **kwargs):
        """Fetches a single message from the mailbox, specified by the
        given X-GM-MSGID.

        Arguments:
            gm_id -- a numeric, globally unique identifier for a gmail message

        Keyword Args:
            full         -- Whether to fetch the entire message, instead of
                            just the headers.  Note that if only_uids is True,
                            this parameter will have no effect.
            teaser       -- Whether to fetch just a brief, teaser version of the
                            body (ie the first mime section).  Note that this
                            option is incompatible with the full
                            option, and the former will take precedence

        Returns:
            A pygmail.message.Message object representing the email message, or
            None if none could be found.  If an error is encountered, an
            IMAPError object will be returned.
        """
        def _on_fetch(message):
            return _cmd(callback, message)

        @pygmail.errors.check_imap_response(callback)
        def _on_search_complete(imap_response):
            data = extract_data(imap_response)
            if len(data) == 0 or not data[0]:
                return _cmd(callback, None)
            else:
                uid = data[0]
                return _cmd_cb(self.fetch, _on_fetch, bool(callback),
                               uid, full=full, **kwargs)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            return _cmd_cb(connection.uid, _on_search_complete, bool(callback),
                           'search', None, 'X-GM-MSGID', gm_id)

        @pygmail.errors.check_imap_response(callback)
        def _on_select(result):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        return _cmd_cb(self.select, _on_select, bool(callback))

    def messages_by_id(self, ids, only_uids=False, full=False, callback=None, **kwargs):
        """Fetches messages in the mailbox by their id

        Returns a list of all messages in the current mailbox that match
        any of the provided ids.

        Args:
            ids          -- A list of zero or more email ids, which should match
                            messages in the current mailbox

        Keyword Args:
            only_uids    -- If True, only the UIDs for the given volitile
                            message ids will be returned, instead of the entire
                            populated GmailMessage object
            full         -- Whether to fetch the entire message, instead of
                            just the headers.  Note that if only_uids is True,
                            this parameter will have no effect.
            only_teasers -- Whether to fetch just a brief, teaser version of the
                            body (ie the first mime section).  Note that this
                            option is incompatible with the full
                            option, and the former will take precedence

        Returns:
            A list of zero or more message objects (or uids) if success, and
            an error object in all other situations
        """
        teasers = kwargs.get("teaser")
        gm_ids = kwargs.get('gm_ids')

        # If we were told to fetch no messages, fast "callback" and don't
        # bother doing any network io
        if len(ids) == 0:
            return _cmd(callback, [])

        @pygmail.errors.check_imap_response(callback)
        def _on_fetch(imap_response):
            data = extract_data(imap_response)
            if only_uids:
                uids = [string.split(elm, " ")[4][:-1] for elm in data]
                return _cmd(callback, uids)
            else:
                messages = parse_fetch_request(data, self, teasers, full, gm_ids)
                return _cmd(callback, messages)

        @pygmail.errors.check_imap_state(callback)
        def _on_connection(connection):
            if gm_ids:
                request = imap_queries["gm_id"]
            elif only_uids:
                request = imap_queries["uid"]
            elif full:
                request = imap_queries["body"]
            elif teasers:
                request = imap_queries["teaser"]
            else:
                request = imap_queries["header"]
            return _cmd_cb(connection.fetch, _on_fetch, bool(callback),
                           ",".join(ids), request)

        @pygmail.errors.check_imap_response(callback)
        def _on_select(result):
            return _cmd_cb(self.account.connection, _on_connection, bool(callback))

        return _cmd_cb(self.select, _on_select, bool(callback))
