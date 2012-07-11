from django.template import RequestContext
from django.http import HttpResponse, Http404
from django.shortcuts import render_to_response, get_object_or_404
from django.db import connection
from django.db.models import Q

import urllib
import re
from datetime import datetime, timedelta

from models import *

def get_all_groups_and_lists(listid=None):
	# Django doesn't (yet) support traversing the reverse relationship,
	# so we'll get all the lists and rebuild it backwards.
	lists = List.objects.select_related('group').all().order_by('listname')
	listgroupid = None
	groups = {}
	for l in lists:
		if l.listid == listid:
			listgroupid = l.group.groupid

		if groups.has_key(l.group.groupid):
			groups[l.group.groupid]['lists'].append(l)
		else:
			groups[l.group.groupid] = {
				'groupid': l.group.groupid,
				'groupname': l.group.groupname,
				'sortkey': l.group.sortkey,
				'lists': [l,],
				'homelink': l.listname,
				}

	return (sorted(groups.values(), key=lambda g: g['sortkey']), listgroupid)


class NavContext(RequestContext):
	def __init__(self, request, listid=None, all_groups=None):
		RequestContext.__init__(self, request)

		if all_groups:
			groups = all_groups
		else:
			(groups, listgroupid) = get_all_groups_and_lists(listid)
		for g in groups:
			# On the root page, remove *all* entries
			# On other lists, remove the entries in all groups other than our
			# own.
			if (not listid) or listgroupid != g['groupid']:
				# Root page, so remove *all* entries
				g['lists'] = []

		self.update({'listgroups': groups})

def index(request):
	(groups, listgroupid) = get_all_groups_and_lists()
	return render_to_response('index.html', {
			'groups': [{'groupname': g['groupname'], 'lists': g['lists']} for g in groups],
			}, NavContext(request, all_groups=groups))

def monthlist(request, listname):
	l = get_object_or_404(List, listname=listname)
	curs = connection.cursor()
	curs.execute("SELECT year, month FROM list_months WHERE listid=%(listid)s ORDER BY year DESC, month DESC", {'listid': l.listid})
	months=[{'year':r[0],'month':r[1], 'date':datetime(r[0],r[1],1) }for r in curs.fetchall()]

	return render_to_response('monthlist.html', {
			'list': l,
			'months': months,
			}, NavContext(request, l.listid))

def render_datelist_from(request, l, d, title, to=None):
	datefilter = Q(date__gte=d)
	if to:
		datefilter.add(Q(date__lt=to), Q.AND)

	mlist = Message.objects.defer('bodytxt', 'cc', 'to').select_related().filter(datefilter).extra(where=["threadid IN (SELECT threadid FROM list_threads WHERE listid=%s)" % l.listid]).order_by('date')[:200]

	threads = set([m.threadid for m in mlist])
	r = render_to_response('datelist.html', {
			'list': l,
			'messages': list(mlist),
			'title': title,
			}, NavContext(request, l.listid))
	r['X-pgthread'] = ":%s:" % (":".join([str(t) for t in threads]))
	return r

def render_datelist_to(request, l, d, title):
	# Need to sort this backwards in the database to get the LIMIT applied
	# properly, and then manually resort it in the correct order. We can do
	# the second sort safely in python since it's not a lot of items..
	mlist = sorted(Message.objects.defer('bodytxt', 'cc', 'to').select_related().filter(date__lte=d).extra(where=["threadid IN (SELECT threadid FROM list_threads WHERE listid=%s)" % l.listid]).order_by('-date')[:200], key=lambda m: m.date)

	threads = set([m.threadid for m in mlist])
	r = render_to_response('datelist.html', {
			'list': l,
			'messages': list(mlist),
			'title': title,
			}, NavContext(request, l.listid))
	r['X-pgthread'] = ":%s:" % (":".join([str(t) for t in threads]))
	return r

def datelistsince(request, listname, msgid):
	l = get_object_or_404(List, listname=listname)
	msg = get_object_or_404(Message, messageid=msgid)
	return render_datelist_from(request, l, msg.date, "%s since %s" % (l.listname, msg.date.strftime("%Y-%m-%d %H:%M:%S")))

def datelistsincetime(request, listname, year, month, day, hour, minute):
	l = get_object_or_404(List, listname=listname)
	d = datetime(int(year), int(month), int(day), int(hour), int(minute))
	return render_datelist_from(request, l, d, "%s since %s" % (l.listname, d.strftime("%Y-%m-%d %H:%M")))

def datelistbefore(request, listname, msgid):
	l = get_object_or_404(List, listname=listname)
	msg = get_object_or_404(Message, messageid=msgid)
	return render_datelist_to(request, l, msg.date, "%s before %s" % (l.listname, msg.date.strftime("%Y-%m-%d %H:%M:%S")))

def datelistbeforetime(request, listname, year, month, day, hour, minute):
	l = get_object_or_404(List, listname=listname)
	d = datetime(int(year), int(month), int(day), int(hour), int(minute))
	return render_datelist_to(request, l, d, "%s before %s" % (l.listname, d.strftime("%Y-%m-%d %H:%M")))


def datelist(request, listname, year, month):
	l = get_object_or_404(List, listname=listname)
	d = datetime(int(year), int(month), 1)
	enddate = d+timedelta(days=31)
	enddate = datetime(enddate.year, enddate.month, 1)
	return render_datelist_from(request, l, d, "%s - %s %s" % (l.listname, d.strftime("%B"), d.year), enddate)


def attachment(request, attid):
	# Use a direct query instead of django, since it has bad support for
	# bytea
	curs = connection.cursor()
	curs.execute("SELECT filename, contenttype, attachment FROM attachments WHERE id=%(id)s", { 'id': int(attid)})
	r = curs.fetchall()
	if len(r) != 1:
		return HttpResponse("Attachment not found")

	return HttpResponse(r[0][2], mimetype=r[0][1])

def _build_thread_structure(threadid):
	# Yeah, this is *way* too complicated for the django ORM
	curs = connection.cursor()
	curs.execute("""WITH RECURSIVE t(id, _from, subject, date, messageid, has_attachment, parentid, datepath) AS(
  SELECT id,_from,subject,date,messageid,has_attachment,parentid,array[]::timestamptz[] FROM messages m WHERE m.threadid=%(threadid)s AND parentid IS NULL
 UNION ALL
  SELECT m.id,m._from,m.subject,m.date,m.messageid,m.has_attachment,m.parentid,t.datepath||t.date FROM messages m INNER JOIN t ON t.id=m.parentid WHERE m.threadid=%(threadid)s
)
SELECT id,_from,subject,date,messageid,has_attachment,parentid,datepath FROM t ORDER BY datepath||date
""", {'threadid': threadid})
	lastpath = []
	for id,_from,subject,date,messageid,has_attachment,parentid,parentpath in curs.fetchall():
		yield {'id':id, 'mailfrom':_from, 'subject': subject, 'printdate': date.strftime("%Y-%m-%d %H:%M:%S"), 'messageid': messageid, 'hasattachment': has_attachment, 'parentid': parentid, 'indent': "&nbsp;" * len(parentpath)}

def message(request, msgid):
	try:
		m = Message.objects.get(messageid=msgid)
	except Message.DoesNotExist, e:
		raise Http404('Message does not exist')

	lists = List.objects.extra(where=["listid IN (SELECT listid FROM list_threads WHERE threadid=%s)" % m.threadid]).order_by('listname')
	threadstruct = list(_build_thread_structure(m.threadid))
	responses = [t for t in threadstruct if t['parentid']==m.id]
	if m.parentid:
		for t in threadstruct:
			if t['id'] == m.parentid:
				parent = t
				break
	else:
		parent = None
	r = render_to_response('message.html', {
			'msg': m,
			'threadstruct': threadstruct,
			'responses': responses,
			'parent': parent,
			'lists': lists,
			}, NavContext(request, lists[0].listid))
	r['X-pgthread'] = ":%s:" % m.threadid
	return r

def message_flat(request, msgid):
	try:
		msg = Message.objects.get(messageid=msgid)
	except Message.DoesNotExist, e:
		raise Http404('Message does not exist')
	allmsg = Message.objects.filter(threadid=msg.threadid).order_by('date')
	# XXX: need to get the complete list of lists!

	r = render_to_response('message_flat.html', {
			'msg': msg,
			'allmsg': allmsg,
			}, NavContext(request))
	r['X-pgthread'] = ":%s:" % msg.threadid
	return r

def message_raw(request, msgid):
	curs = connection.cursor()
	curs.execute("SELECT threadid, rawtxt FROM messages WHERE messageid=%(messageid)s", {
			'messageid': msgid,
			})
	row = curs.fetchall()
	if len(row) != 1:
		raise Http404('Message does not exist')

	r = HttpResponse(row[0][1], content_type='text/plain')
	r['X-pgthread'] = ":%s:" % row[0][0]
	return r

def testview(request, seqid):
	m = Message.objects.get(pk=seqid)
	try:
		nextm = Message.objects.filter(id__gt=m.id).order_by('id')[0]
	except IndexError:
		nextm = None
	try:
		prevm = Message.objects.filter(id__lt=m.id).order_by('-id')[0]
	except IndexError:
		prevm = None

	return render_to_response('test.html', {
			'msg': m,
			'nextmsg': nextm,
			'prevmsg': prevm,
			})


def oldsite(request, msgid):
	u = urllib.urlopen('http://archives.postgresql.org/message-id/%s' % msgid)
	m = re.search('<!--X-Body-of-Message-->(.*)<!--X-Body-of-Message-End-->', u.read(), re.DOTALL)
	return HttpResponse(m.groups(1), content_type='text/html')
