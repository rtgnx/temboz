#!/usr/local/bin/python
import sys, os, stat, logging, base64, time, imp, gzip, traceback, pprint, csv
import threading, BaseHTTPServer, SocketServer, cStringIO, urlparse, urllib
import flask, sqlite3, string, requests, re, datetime, hmac, passlib.hash
import hashlib, socket, json, hmac, werkzeug
import param, update, filters, util, normalize, dbop, social, fts5, __main__

# HTTP header to force caching
no_expire = [
  'Expires: Thu, 31 Dec 2037 23:55:55 GMT',
  'Cache-Control: max_age=2592000'
]

########################################################################

whitelist = {'/login', '/opml', '/_share', '/blogroll.json'}
try:
  cookie_secret = os.urandom(16)
except NotImplementedError:
  import random
  cookie_secret = ''.join(str(random.random()) for x in range(8))
class AuthWrapper:
  """HTTP Basic Authentication WSGI middleware for Pageserver"""
  def __init__(self, application):
    self.application = application

  def __call__(self, environ, start_response):
    url = environ['PATH_INFO'].rstrip('/')
    if url in whitelist or url.startswith('/static/'):
      return self.application(environ, start_response)
    back_url = url if url else '/'
    cookies = werkzeug.utils.parse_cookie(environ)
    auth_cookie = cookies.get('auth')
    auth_login = None
    if cookie_secret and auth_cookie:
      auth = auth_cookie.split(':', 1)
      if len(auth) == 2:
        login, hash = auth
        if login == param.settings['login'] \
           and hash == hmac.new(cookie_secret, login,
                                hashlib.sha256).hexdigest():
          auth_login = login
    
    if not auth_login:
      start_response('302 Moved',
                     [('Location', '/login?back=' + urllib.quote_plus(url)),
                      ('Content-Type', 'text/html')])
      return '<h1>Moved</h1>'
    return self.application(environ, start_response)

# seed for CSRF protection nonces
nonce_seed = os.urandom(20)
def gen_nonce(msg):
  return hmac.new(nonce_seed, msg).hexdigest()

def check_nonce(msg, nonce):
  #return hmac.compare_digest(gen_nonce(msg), nonce.decode('hex'))
  return gen_nonce(msg) == nonce
  
app = flask.Flask(__name__)
app.wsgi_app = AuthWrapper(app.wsgi_app)
app.debug = getattr(param, 'debug', False)
if not app.debug:
  # this setting interferes with Flask debug
  socket.setdefaulttimeout(10)
#app.jinja_options = {'extensions': ['jinja2.ext.do']}
app.jinja_env.trim_blocks=True
app.jinja_env.lstrip_blocks=True

def change_param(*arg, **kwargs):
  parts = urlparse.urlparse(flask.request.full_path)
  parts = list(parts)
  param = urlparse.parse_qs(parts[4])
  param.update(kwargs)
  parts[4] = urllib.urlencode(param, True)
  return urlparse.urlunparse(tuple(parts))

########################################################################
# utility functions
def since(delta_t):
  if not delta_t:
    return 'never'
  delta_t = float(delta_t)
  if delta_t < 2.0/24:
    return str(int(delta_t * 24.0 * 60.0)) + ' minutes ago'
  elif delta_t < 1.0:
    return str(int(delta_t * 24.0)) + ' hours ago'
  elif delta_t < 2.0:
    return 'one day ago'
  elif delta_t < 3.0:
    return str(int(delta_t)) + ' days ago'
  else:
    return time.strftime('%Y-%m-%d',
                         time.localtime(time.time() - 86400 * delta_t))
# escaping support
ent_re = re.compile('([&<>"\'\x80-\xff])')
# we do not support all HTML entities as only these are defined in XML
ent_dict_xml = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&apos;'
  }
def ent_sub_xml(m):
  c = m.groups()[0]
  return ent_dict_xml.get(c, '&#%d;' % ord(c))
def atom_content(content):
  # XXX should strip out tags that are inappropriate in a RSS reader
  # XXX such as <script>
  # see:
  #http://diveintomark.org/archives/2003/06/12/how_to_consume_rss_safely.html
  return ent_re.sub(ent_sub_xml, content)

# validate parameter names to guard against XSS attacks
valid_chars = set(string.letters + string.digits + '_')
def regurgitate_except(*exclude):
  """Regurgitate query string parameters as <input type="hidden"> fields
    to help maintain context in self-submitting forms"""
  opts = {}
  for k in flask.request.form:
    opts[k] = flask.request.form[k]
  return '\n'.join('<input type="hidden" name="%s" value="%s">'
                   % (name, urllib.quote_plus(value.encode('utf-8')))
                   for (name, value) in opts.iteritems()
                   if set(name).issubset(valid_chars)
                   and name not in ('referer', 'headers')
                   and name not in exclude)

########################################################################
# main loop of the server
def run():
  # force loading of the database so we don't have to wait an hour to detect
  # a database format issue
  with dbop.db() as db:
    c = db.cursor()
    dbop.load_settings(c)
    c.close()
  
  logging.getLogger().setLevel(logging.INFO)
  # start Flask
  app.run(host=param.settings['ip'],
          port=int(param.settings['port']),
          threaded=True)

########################################################################
# actual request handlers

@app.route("/login", methods=['GET', 'POST'])
def login(): 
  if flask.request.method == 'POST':
    f = flask.request.form
    login = f.get('login')
    if login == param.settings['login'] \
       and passlib.hash.argon2.verify(f.get('password', ''),
                                      param.settings['passwd']):
      # set auth cookie
      cookie = login + ':' + hmac.new(cookie_secret, login,
                                      hashlib.sha256).hexdigest()
      back = flask.request.args.get('back', '/')
      back = back if back else '/'
      resp = flask.make_response(
        flask.redirect(back))
      resp.set_cookie('auth', cookie, httponly=True)
      return resp
    else:
      return flask.redirect('/login?err=invalid+login+or+password')
  else:
    return flask.render_template('login.html',
                                 err=flask.request.args.get('err'))

@app.route("/")
@app.route("/view")
def view(): 
  # Query-string parameters for this page
  #   show
  #   feed_uid
  #   search
  #   where_clause
  #   min, max (item UID)
  #
  # What items to use
  #   unread:   unread articles (default)
  #   up:       articles already flagged interesting
  #   down:     articles already flagged uninteresting
  #   filtered: filtered out articles
  #   mylos:    read-only view, e.g. http://www.majid.info/mylos/temboz.html
  with dbop.db() as c:
    filters.load_rules(c)
    show = flask.request.args.get('show', 'unread')
    i = update.ratings_dict.get(show, 1)
    show = update.ratings[i][0]
    item_desc = update.ratings[i][1]
    where = update.ratings[i][3]
    sort = flask.request.args.get('sort', 'seen')
    i = update.sorts_dict.get(sort, 1)
    sort = update.sorts[i][0]
    sort_desc = update.sorts[i][1]
    order_by = update.sorts[i][3]
    # optimizations for mobile devices
    mobile = bool(flask.request.args.get('mobile', False))
    # SQL options
    params = []
    # filter by filter rule ID
    if show == 'filtered':
      try:
        params.append(int(flask.request.args['rule_uid']))
        where += ' and item_rule_uid=?'
      except:
        pass
    # filter by uid range
    try:
      params.append(int(flask.request.args['min']))
      where += ' and item_uid >= ?'
    except:
      pass
    try:
      params.append(int(flask.request.args['max']))
      where += ' and item_uid <= ?'
    except:
      pass
    # Optionally restrict view to a single feed
    feed_uid = None
    try:
      feed_uid = int(flask.request.args['feed_uid'])
      params.append(feed_uid)
      where +=  ' and item_feed_uid=?'
    except:
      pass
    # search functionality using fts5 if available
    search = flask.request.args.get('search')
    search_in = flask.request.args.get('search_in', 'title')
    if search:
      if dbop.fts_enabled:
        where += """ and item_uid in (
          select rowid from search where %s '%s'
        )""" % ('item_title match' if search_in == 'title' else 'search=',
                fts5.fts5_term(search))
      else:
        search = search.lower()
        search_where = 'item_title' if search_in == 'title' else 'item_content'
        where += ' and lower(%s) like ?' % search_where
        if type(search) == unicode:
          # XXX vulnerable to SQL injection attack
          params.append('%%%s%%' % search.encode('ascii', 'xmlcharrefreplace'))
        else:
          params.append('%%%s%%' % search)
        # Support for arbitrary where clauses in the view script. Not directly
        # accessible from the UI
        extra_where = flask.request.args.get('where_clause')
        if extra_where:
          # XXX vulnerable to SQL injection attack
          where += ' and %s' % extra_where
    # Preliminary support for offsets to read more than overload_threshold
    # articles, not fully implemented yet
    try:
      offset = int(flask.request.args['offset'])
    except:
      offset = 0
    ratings_list = ''.join(
      '<li><a href="%s">%s</a></li>' % (change_param(show=rating_name),
                                        rating_desc)
      for (rating_name, rating_desc, discard, discard) in update.ratings)
    sort_list = ''.join(
      '<li><a href="%s">%s</a></li>' % (change_param(sort=sort_name),
                                        sort_desc)
      for (sort_name, sort_desc, discard, discard) in update.sorts)
    # fetch and format items
    tag_dict, rows = dbop.view_sql(c, where, order_by, params,
                                   param.overload_threshold)
    items = []
    for row in rows:
      (uid, creator, title, link, content, loaded, created, rated,
       delta_created, rating, filtered_by, feed_uid, feed_title, feed_html,
       feed_xml, feed_snr) = row
      # redirect = '/redirect/%d' % uid
      redirect = link
      since_when = since(delta_created)
      creator = creator.replace('"', '\'')
      if rating == -2:
        if filtered_by:
          rule = filters.Rule.registry.get(filtered_by)
          if rule:
            title = rule.highlight_title(title)
            content = rule.highlight_content(content)
          elif filtered_by == 0:
            content = '%s<br><p>Filtered by feed-specific Python rule</p>' \
                      % content
      if uid in tag_dict or (creator and (creator != 'Unknown')):
        # XXX should probably escape the Unicode here
        tag_info = ' '.join('<span class="item tag">%s</span>' % t
                            for t in sorted(tag_dict.get(uid, [])))
        if creator and creator != 'Unknown':
          tag_info = '%s<span class="author tag">%s</span>' \
                     % (tag_info, creator)
        tag_info = '<div class="tag_info" id="tags_%s">' % uid \
                   + tag_info + '</div>'
        tag_call = '<a href="javascript:toggle_tags(%s);">tags</a>' % uid
      else:
        tag_info = ''
        tag_call = '(no tags)'
      items.append({
        'uid': uid,
        'since_when': since_when,
        'creator': creator,
        'loaded': loaded,
        'feed_uid': feed_uid,
        'title': title,
        'feed_html': feed_html,
        'content': content,
        'tag_info': tag_info,
        'tag_call': tag_call,
        'redirect': redirect,
        'feed_title': feed_title,
      })

    return flask.render_template('view.html', show=show, item_desc=item_desc,
                                 feed_uid=feed_uid, ratings_list=ratings_list,
                                 sort_desc=sort_desc, sort_list=sort_list,
                                 items=items,
                                 overload_threshold=param.overload_threshold)

@app.route("/xmlfeedback/<op>/<rand>/<arg>")
def ajax(op, rand, arg):
  item_uid = arg.split('.')[0]
  # for safety, these operations should be idempotent
  if op in ['promote', 'demote', 'basic', 'yappi']:
    if op != 'yappi':
      update.set_rating(int(item_uid), {
        'demote': -1,
        'basic': 0,
        'promote': 1
      }[op])
      return '<?xml version="1.0"?><nothing />'
    else:
      import yappi
      assert arg in ['start', 'stop', 'clear_stats']
      getattr(yappi, arg)()
  return '<?xml version="1.0"?><nothing />'
  
@app.route("/robots.txt")
def robots():
  return ('User-agent: *\nDisallow: /\n', 200, {'Content-Type': 'text/plain'})

@app.route("/favicon.ico")
@app.route("/api/favicon.ico")
@app.route("/apple-touch-icon.png")
@app.route("/api/apple-touch-icon.png")
def favicon():
  return ('No favicon\n', 404, {'Content-Type': 'text/plain'})

def rule_tabset(feed_uid=None):

    return flask.render_template(
      'feed.html', filters=filters,
      len=len, max=max, **locals()
    )

@app.template_filter('rule_lines')
def rule_lines(text):
  return max(4, filters.rule_lines(text))
  
@app.route("/feed/<int:feed_uid>", methods=['GET', 'POST'])
@app.route("/feed/<int:feed_uid>/<op>", methods=['GET', 'POST'])
def feed_info(feed_uid, op=None):
  notices = []
  # operations
  if op:
    if op == 'activate':
      status = 0
      update.set_status(feed_uid, status)
    elif op == 'suspend':
      status = 1
      update.set_status(feed_uid, status)
    elif op == 'private':
      private = 1
      update.update_feed_private(feed_uid, private)
    elif op == 'public':
      private = 0
      update.update_feed_private(feed_uid, private)
    elif op == 'Dupcheck':
      dupcheck = 1
      update.update_feed_dupcheck(feed_uid, dupcheck)
    elif op == 'NoDupcheck':
      dupcheck = 0
      update.update_feed_dupcheck(feed_uid, dupcheck)
    elif op == 'exempt':
      exempt = 1
      update.update_feed_exempt(feed_uid, exempt)
    elif op == 'reinstate':
      exempt = 0
      update.update_feed_exempt(feed_uid, exempt)
    elif op == 'catchup' and flask.request.form.get('confirm') == 'yes':
      update.catch_up(feed_uid)
      back = flask.request.args.get('back', '')
      if back == '/feeds':
        return flask.redirect(back)
      notices.append('<p>Caught up successfully.</p>')
  with dbop.db() as c:
    # Get feed statistics
    row = dbop.feed_info_sql(c, feed_uid).fetchone()
    (feed_title, feed_desc, feed_filter, feed_html, feed_xml, feed_pubxml,
     delta_t, interesting, unread, uninteresting, filtered, total, status,
     private, exempt, dupcheck, feed_errors) = row
    feed_pubxml = feed_pubxml or ''
    feed_filter = feed_filter or ''
    since_when = since(delta_t)
    unread = int(unread)
    interesting = int(interesting)
    uninteresting = int(uninteresting)
    filtered = int(filtered)
    total = int(total)
    if interesting + uninteresting > 0:
      ratio = interesting * 100 / (interesting + uninteresting)
    else:
      ratio = 0
      assert interesting + uninteresting + unread + filtered == total, \
        feed_title
      uninteresting = total - unread - filtered - interesting
      if feed_filter is None:
        feed_filter = ''
    # hard purge confirmation
    if op == 'hardpurge' and flask.request.form.get('confirm') == 'yes':
      status = update.hard_purge(feed_uid)
      if status:
        notices.append('<p>Error: %r</p>' % status)
      else:
        notices.append('<p>Deleted <a href="%s">%s</a></p>'
                       % (feed_html, feed_title))
    # Change feed title/html/desc/filter if requested
    f = flask.request.form
    if flask.request.method == 'POST':
      if f.get('feed_title') and f.get('feed_title') != feed_title:
        feed_title = f.get('feed_title')
        update.update_feed_title(feed_uid, feed_title)
        notices.append('<p>Feed title updated successfully.</p>')
      if f.get('feed_html') and f.get('feed_html') != feed_html:
        feed_html = f.get('feed_html')
        update.update_feed_html(feed_uid, feed_html)
        notices.append('<p>Feed HTML link updated successfully.</p>')
      if f.get('feed_desc') and f.get('feed_desc') != feed_desc:
        feed_desc = f.get('feed_desc')
        update.update_feed_desc(feed_uid, feed_desc)
        notices.append('<p>Feed description updated successfully.</p>')
      if f.get('feed_filter') and f.get('feed_filter') != feed_filter:
        feed_filter = f.get('feed_filter')
        update.update_feed_filter(feed_uid, feed_filter)
        notices.append('<p>Feed filter updated successfully.</p>')
      if f.get('feed_pubxml') and f.get('feed_pubxml') != feed_pubxml:
        feed_pubxml = f.get('feed_pubxml')
        update.update_feed_pubxml(feed_uid, feed_pubxml)
        notices.append('<p>Feed public XML link updated successfully.</p>')
    # Change feed URL if requested
    if op == 'refresh' or (
        flask.request.method == 'POST'
        and flask.request.form.get('feed_xml')
        and flask.request.form.get('feed_xml') != feed_xml
    ):
      try:
        num_added, num_filtered = update.update_feed_xml(
          feed_uid, flask.request.form.get('feed_xml', feed_xml))
        unread += num_added
        filtered += num_filtered
        feed_errors = 0
        notices.append('<p>Feed refreshed successfully.</p>')
        if status == 1:
          status = 0
          update.set_status(feed_uid, status)
          notices.append('<p>Feed reactivated</p>')
        if num_added > 0:
          notices.append("""<p>%d new unread articles.&nbsp;&nbsp;
    <a href="/view?feed_uid=%d">view articles now</a>&nbsp;&nbsp;
    <a href="/feed/%d/catchup">catch up</a></p>"""
                         % (unread, feed_uid, feed_uid))
      except update.ParseError:
        notices.append('<p><b>Connection or parse error in attempting to'
                       + 'subscribe to</b> %s, check URL</p>' % feed_xml)
      except update.FeedAlreadyExists:
        notices.append('<p>The feed %s ' % feed_xml
                       + 'is already assigned to another feed,'
                       + 'check for duplicates.</p>')
      except update.UnknownError, e:
        notices.append('<p>Unknown error:<p>\n<pre>%s</pre>\n' % e.args[0])
    feed_public = None
    hidden = regurgitate_except()
    # Display feed flags with option to change it
    if status == 0:
      status_text = 'Active'
      status_change_op = 'suspend'
    elif status == 1:
      status_text = 'Suspended'
      status_change_op = 'activate'
    else:
      status_text = 'Unknown'
      status_change_op = 'activate'
    if private == 0:
      private_text = 'Public'
      private_change_op = 'private'
    elif private == 1:
      private_text = 'Private'
      private_change_op = 'public'
    else:
      private_text = 'Unknown'
      private_change_op = 'private'
    if exempt == 0:
      exempt_text = 'Not exempt'
      exempt_change_op = 'exempt'
    elif exempt == 1:
      exempt_text = 'Exempt'
      exempt_change_op = 'reinstate'
    else:
      exempt_text = 'Unknown'
      exempt_change_op = 'exempt'
    # Get top rules
    top_rules = dbop.top_rules(c, feed_uid)
    feed_rules = dbop.rules(c, feed_uid)
    
    return flask.render_template(
      'feed.html', filters=filters,
      len=len, max=max, **locals()
    )

@app.route("/feeds")
def feeds(): 
  sort_key = flask.request.args.get('sort', '(unread > 0) DESC, snr')
  if sort_key == 'feed_title':
    sort_key = 'lower(feed_title)'
  order = flask.request.args.get('order', 'DESC')
  with dbop.db() as db:
    rows = dbop.feeds(db, sort_key, order)
    sum_unread      = sum(int(row['unread']) for row in rows)
    sum_filtered    = sum(int(row['filtered']) for row in rows)
    sum_interesting = sum(int(row['interesting']) for row in rows)
    sum_total       = sum(int(row['total']) for row in rows)
    return flask.render_template('feeds.html',
                                 since=since, int=int, repr=repr,
                                 **locals())

@app.route("/rules")
def rules(): 
  with dbop.db() as db:
    c = db.cursor()
    feed_rules = dbop.rules(c, None)
    
    return flask.render_template(
      'rules.html', filters=filters,
      len=len, max=max, **locals()
    )

@app.route("/rule/<int:rule_uid>/<op>")
def rule_op(rule_uid, op): 
  with dbop.db() as db:
    c = db.cursor()
    if op == 'del':
      filters.del_kw_rule(db, c, rule_uid)
  return '<?xml version="1.0"?><nothing />'

@app.route("/rule/add", methods=['POST'])
def rule_add(): 
  with dbop.db() as db:
    c = db.cursor()
    filters.add_kw_rule(db, c, **(flask.request.form.to_dict()))
    db.commit()
    return '<?xml version="1.0"?><nothing />'

@app.route("/add", methods=['GET', 'POST'])
def add_feed(): 
  if flask.request.method == 'POST':
    feed_xml = flask.request.form.get('feed_xml', '').strip()
    if feed_xml:
      with dbop.db() as db:
        c = db.cursor()
        try:
          feed_uid, feed_title, num_added, num_filtered \
            = update.add_feed(feed_xml)
        except update.ParseError:
          feed_err = 'Connection or parse error in subcription attempt.'
          resolution= 'check URL'
        except update.AutodiscoveryParseError:
          feed_err = 'Autodiscovery failed.'
          resolution = 'you need to find a valid feed URL'
        except update.FeedAlreadyExists:
          feed_err = 'The feed URL is already assigned to another feed.'
          resolution = 'check for duplicates'
        except requests.exceptions.RequestException as e:
          feed_err = 'Error loading URL during autodiscovery attempt: %r' % e
        except update.UnknownError, e:
          feed_err = 'Unknown error: %r' % e.args[0]
    
  return flask.render_template(
    'add.html', filters=filters,
    len=len, max=max, **locals()
  )

@app.route("/settings", methods=['GET', 'POST'])
def settings(status=''): 
  op = flask.request.form.get('op', '') or flask.request.args.get('op', '')
  with dbop.db() as db:
    c = db.cursor()

    if op == 'refresh':
      __main__.updater.event.set()
      status = 'Manual refresh of all feeds requested.'
    elif op == 'debug':
      if flask.request.form.get('debug', '') == 'Disable verbose logging':
        setattr(param, 'debug', False)
      else:
        setattr(param, 'debug', True)
    elif op == 'facebook':
      api_key = flask.request.form.get('api_key', '').strip()
      if api_key:
        dbop.setting(db, c, fb_api_key=api_key)
      app_id = flask.request.form.get('app_id', '').strip()
      if app_id:
        dbop.setting(db, c, fb_app_id=app_id)
      fb_secret = flask.request.form.get('fb_secret', '').strip()
      if fb_secret:
        dbop.setting(db, c, fb_secret=fb_secret)
    elif op == 'del_token':
      dbop.setting(db, c, fb_token='')
    elif op == 'maint':
      dbop.snr_mv(db, c)
      db.commit()

    stats = filters.stats(c)
    
    return flask.render_template(
      'settings.html', filters=filters,
      executable=sys.argv[0], py_version=sys.version,
      param_debug=param.debug, param_settings=param.settings,
      started=__main__.started,
      uptime=datetime.datetime.now()-__main__.started,
      len=len, max=max, **locals()
    )

@app.route("/stats")
def stats(): 
  with dbop.db() as db:
    c = db.cursor()
    rows = dbop.stats(c)
    csvfile = cStringIO.StringIO()
    out = csv.writer(csvfile, dialect='excel', delimiter=',')
    out.writerow([col[0].capitalize() for col in c.description])
    for row in c:
      out.writerow(row)
    try:
      return (csvfile.getvalue(), 200, {'Content-Type': 'text/csv'})
    finally:
      csvfile.close()

@app.route("/facebook")
def facebook():
  with dbop.db() as db:
    c = db.cursor()
    app_id = param.settings.get('fb_app_id', '')
    secret = param.settings.get('fb_secret', '')
    if app_id and secret:
      ## XXX cannot assume https://
      redir = 'https://' + flask.request.headers['host'] \
              + '/facebook?op=oauth_redirect'
      op = flask.request.args.get('op', '')
      if not op:
        fb_url = 'https://graph.facebook.com/oauth/authorize?display=touch&client_id=' + app_id + '&scope=publish_actions&redirect_uri=' + redir
        print >> param.log, 'FB_URL =', fb_url
        return flask.redirect(fb_url)
      elif op == 'oauth_redirect':
        code = flask.request.args.get('code', '')
        if code:
          r = requests.get(
            'https://graph.facebook.com/oauth/access_token',
            params={
              'client_id': app_id,
              'client_secret': secret,
              'code': code,
              'redirect_uri': redir
            }
          )
          print >> param.log, 'FACEBOOK TOKEN RESPONSE', r.text
          if r.text.startswith('{'):
            token = json.loads(r.text).get('access_token')
          else:
            token = r.text.split('access_token=', 1)[-1]
          dbop.setting(db, c, fb_token=token)
          return flask.redirect('/settings#facebook')
    else:
      return settings(status='You need to set the App ID first')

@app.route("/_share")
def mylos():
  with dbop.db() as db:
    c = db.cursor()
    last = dbop.share(c)
    return flask.render_template(
      '_share.atom', time=time, normalize=normalize,
      atom_content=atom_content, **locals()
    )

@app.route("/threads")
def threads():
  frames = sys._current_frames()
  try:
    return flask.render_template(
      'threads.html', sys=sys, pprint=pprint, traceback=traceback,
      sorted=sorted, **locals()
    )
  finally:
    del frames

@app.route("/stem")
def stem():
  term = flask.request.args.get('q', '')
  stem = ' '.join(normalize.stem(normalize.get_words(term)))
  return (stem, 200, {'Content-Type': 'text/plain'})

@app.route("/opml")
def opml():
  sort_key = flask.request.args.get('sort', '(unread > 0) DESC, snr')
  if sort_key == 'feed_title':
    sort_key = 'lower(feed_title)'
  order = flask.request.args.get('order', 'DESC')
  with dbop.db() as db:
    rows = dbop.opml(db)
    return (flask.render_template('opml.opml',
                                  atom_content=atom_content, rows=rows),
            200 , {'Content-Type': 'text/plain'})

@app.route("/item/<int:uid>/<op>", methods=['GET', 'POST'])
def item(uid, op):
  assert op == 'edit'
  status = None
  if flask.request.method == 'POST':
    assert check_nonce('edit%d' % uid, flask.request.form.get('nonce'))
    status = update.update_item(
      uid, *[flask.request.form.get(x) for x in ['href', 'title', 'content']])
  with dbop.db() as db:
    title, content, href = dbop.item(db, uid)
  nonce = gen_nonce('edit%d' % uid)
  return flask.render_template(
    'edit.html',
    normalize=normalize,
    len=len, max=max, **locals()
  )

@app.route("/profile")
def profile():
  import yappi, cStringIO
  if not yappi.is_running():
    yappi.start()
  s = yappi.get_func_stats()
  s = s.sort(flask.request.args.get('sort', 'tsub'))
  f = cStringIO.StringIO()
  s.print_all(f, columns={
    0: ('name', 80),
    1: ('ncall', 20),
    2: ('tsub', 8),
    3: ('ttot', 8)
  })
  if flask.request.args.get('clear', 'N').lower() \
     in {'y', 'yes', 'true', 'on'}:
    yappi.clear_stats()
  return '<pre>\n' + f.getvalue() + '</pre>\n'

@app.route("/blogroll.json")
def blogroll():
  cols = ('uid', 'title', 'description', 'html', 'xml', 'snr')
  with dbop.db() as db:
    rows = dbop.opml(db)
  return (
    json.dumps(
      [dict(zip(cols, row)) for row in rows],
      indent=2
    ),
    200 , {'Content-Type': 'application/json'}
  )
