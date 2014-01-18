"""Datastore model classes.

For the record, these are the remote_api_shell commands I used to do the schema
migration from Comment to Response. *First*, temporarily drop the auto_now=True
on the 'updated' property. Then:

~/google_appengine/remote_api_shell.py -s localhost:8080
OR
~/google_appengine/remote_api_shell.py brid-gy

heaven@gmail.com
...

from models import Comment, Response

for c in Comment.all():
  props = db.to_dict(c)
  props['response_json'] = props.pop('comment_json')
  Response(key_name=c.key().name(), **props).save()

# sanity check
Comment.all().count()
Response.all().count()

Now, add auto_now=True back to the 'updated' property.
"""

import datetime
import itertools
import json
import logging
import urllib
import urlparse

from activitystreams.source import SELF
import appengine_config
import util
from webutil.models import KeyNameModel

from google.appengine.api import mail
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import db


class Site(KeyNameModel):
  """A web site for a single entity, e.g. Facebook profile or WordPress blog.
  """

  # short name for this site type. used in URLs, ec.
  SHORT_NAME = None
  STATUSES = ('enabled', 'disabled', 'error')
  POLL_FREQUENCY = datetime.timedelta(minutes=5)

  created = db.DateTimeProperty(auto_now_add=True, required=True)
  url = db.LinkProperty()
  status = db.StringProperty(choices=STATUSES, default='enabled')

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Site.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    new = cls.new(handler, **kwargs)
    existing = db.get(new.key())
    if existing:
      logging.warning('Overwriting %s %s! Old version:\n%s',
                      existing.label(), new.key(), new.to_xml())
      new_msg = "Updated %s. Refresh to see what's new!" % existing.label()
    else:
      logging.info('Added %s %s %s', new.label(), new.key().name(), new.key())
      new_msg = "Added %s. Refresh to see what we've found!" % new.label()
      mail.send_mail(sender='add@brid-gy.appspotmail.com',
                     to='webmaster@brid.gy',
                     subject='Added Brid.gy user: %s %s' %
                     (new.label(), new.key().name()),
                     body='%s/#%s' % (handler.request.host_url, new.dom_id()))

    handler.messages = {new_msg}

    # TODO: ugh, *all* of this should be transactional
    new.save()
    return new

  def dom_id(self):
    """Returns the DOM element id for this site."""
    return '%s-%s' % (self.SHORT_NAME, self.key().name())


class Source(Site):
  """A silo account, e.g. a Facebook or Google+ account.

  Each concrete silo class should subclass this class.
  """

  AS_CLASS = None  # the corresponding activitystreams-unofficial class
  last_polled = db.DateTimeProperty(default=util.EPOCH)
  last_poll_attempt = db.DateTimeProperty(default=util.EPOCH)

  # full human-readable name
  name = db.StringProperty()
  picture = db.LinkProperty()

  # points to an oauth-dropins auth entity. The model class should be a subclass
  # of oauth_dropins.BaseAuth.
  # the token should be generated with the offline_access scope so that it
  # doesn't expire. details: http://developers.facebook.com/docs/authentication/
  auth_entity = db.ReferenceProperty()

  last_activity_id = db.StringProperty()
  last_activities_etag = db.StringProperty()

  # as_source is *not* set to None by default here, since it needs to be unset
  # for __getattr__ to run when it's accessed.

  def new(self, **kwargs):
    """Factory method. Creates and returns a new instance for the current user.

    To be implemented by subclasses.
    """
    raise NotImplementedError()

  def __getattr__(self, name):
    """Lazily load the auth entity and instantiate self.as_source."""
    if name == 'as_source' and self.auth_entity:
      token = self.auth_entity.access_token()
      if not isinstance(token, tuple):
        token = (token,)
      self.as_source = self.AS_CLASS(*token)
      return self.as_source

    return getattr(super(Source, self), name)

  def label(self):
    """Human-readable label for this site."""
    return '%s (%s)' % (self.name, self.AS_CLASS.NAME)

  def get_activities_response(self, **kwargs):
    """Returns recent posts and embedded comments for this source.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.
    """
    return self.as_source.get_activities_response(group_id=SELF, **kwargs)

  def get_activities(self, *args, **kwargs):
    return self.get_activities_response(*args, **kwargs)['items']

  def get_post(self, id):
    """Returns a post from this source.

    Args:
      id: string, site-specific post id

    Returns: dict, decoded ActivityStreams activity, or None
    """
    activities = self.get_activities(activity_id=id, user_id=self.key().name())
    return activities[0] if activities else None

  def get_comment(self, comment_id, activity_id=None):
    """Returns a comment from this source.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      comment_id: string, site-specific comment id
      activity_id: string, site-specific activity id

    Returns: dict, decoded ActivityStreams comment object, or None
    """
    return self.as_source.get_comment(comment_id, activity_id=activity_id)

  def get_like(self, activity_user_id, activity_id, like_user_id):
    """Returns an ActivityStreams 'like' activity object.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      like_user_id: string id of the user who liked the activity
    """
    return self.as_source.get_like(activity_user_id, activity_id, like_user_id)

  def get_share(self, activity_user_id, activity_id, share_id):
    """Returns an ActivityStreams 'share' activity object.

    Passes through to activitystreams-unofficial by default. May be overridden
    by subclasses.

    Args:
      activity_user_id: string id of the user who posted the original activity
      activity_id: string activity id
      share_id: string id of the share object or the user who shared it
    """
    return self.as_source.get_share(activity_user_id, activity_id, share_id)

  @classmethod
  def create_new(cls, handler, **kwargs):
    """Creates and saves a new Source and adds a poll task for it.

    Args:
      handler: the current RequestHandler
      **kwargs: passed to new()
    """
    new = super(Source, cls).create_new(handler, **kwargs)
    util.add_poll_task(new)
    return new


class Response(KeyNameModel):
  """A comment, like, or repost to be propagated.

  The key name is the commentobject id as a tag URI.
  """
  TYPES = ('comment', 'like', 'repost')
  STATUSES = ('new', 'processing', 'complete', 'error')

  # ActivityStreams JSON activity and comment, like, or repost
  type = db.StringProperty(choices=TYPES, default='comment')
  activity_json = db.TextProperty()
  response_json = db.TextProperty()
  source = db.ReferenceProperty()
  status = db.StringProperty(choices=STATUSES, default='new')
  leased_until = db.DateTimeProperty()
  created = db.DateTimeProperty(auto_now_add=True)
  updated = db.DateTimeProperty(auto_now=True)

  # Original post links, ie webmention targets
  sent = db.StringListProperty()
  unsent = db.StringListProperty()
  error = db.StringListProperty()
  failed = db.StringListProperty()
  skipped = db.StringListProperty(default=[])

  @db.transactional
  def get_or_save(self):
    existing = db.get(self.key())
    if existing:
      # logging.debug('Deferring to existing response %s.', existing.key().name())
      # this might be a nice sanity check, but we'd need to hard code certain
      # properties (e.g. content) so others (e.g. status) aren't checked.
      # for prop in self.properties().values():
      #   new = prop.get_value_for_datastore(self)
      #   existing = prop.get_value_for_datastore(existing)
      #   assert new == existing, '%s: new %s, existing %s' % (prop, new, existing)
      return existing

    obj = json.loads(self.response_json)
    self.type = Response.get_type(obj)
    logging.debug('New response to propagate! %s %s %s', self.type,
                  self.key().id_or_name(), obj.get('url', '[no url]'))

    self.save()
    taskqueue.add(queue_name='propagate',
                  params={'response_key': str(self.key())},
                  # tasks inserted from a backend (e.g. twitter_streaming) are
                  # sent to that backend by default, which doesn't work in the
                  # dev_appserver. setting the target version to 'default' in
                  # queue.yaml doesn't work either, but setting it here does.
                  #
                  # (note the constant. the string 'default' works in
                  # dev_appserver, but routes to default.brid-gy.appspot.com in
                  # prod instead of www.brid.gy, which breaks SSL because
                  # appspot.com doesn't have a third-level wildcard cert.)
                  target=taskqueue.DEFAULT_APP_VERSION)
    return self

  @staticmethod
  def get_type(obj):
    """Returns the response type for an ActivityStreams object."""
    type = obj.get('objectType')
    if type == 'activity' and obj.get('verb') == 'like':
      return 'like'
    elif type == 'activity' and obj.get('verb') == 'share':
      return 'repost'
    else:
      # default to comment. (e.g. Twitter replies technically have objectType note)
      return 'comment'


class Comment(Response):
  """Backward compatibility. TODO: remove.
  """
  comment_json = db.TextProperty()


class DisableSource(Exception):
  """Raised when a user has deauthorized our app inside a given platform.
  """
