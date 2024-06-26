
# Copyright 2008-2014 Jaap Karssenberg <jaap.karssenberg@gmail.com>

'''This module contains a web interface for zim. This is an alternative
to the GUI application.

It can be run either as a stand-alone web server or embedded in another
server as a cgi-bin script or using  one of the python web frameworks
using the "WSGI" API.

The main classes here are L{WWWInterface} which implements the interface
(and is callable as a "WSGI" application) and L{Server} which implements
the standalone server.
'''

# TODO setting for doc_root_url when running in CGI mode
# TODO support "etg" and "if-none-match' headers at least for icons
# TODO: redirect server logging to logging module + set default level to -V in server process


import logging

from functools import partial

from wsgiref.headers import Headers
import urllib.request
import urllib.parse
import urllib.error

from zim.fs import adapt_from_oldfs
from zim.newfs import SEP, FileNotFoundError
from zim.errors import Error
from zim.notebook import Notebook, Path, encode_filename, PageNotFoundError
from zim.config import data_file
from zim.parse.encode import url_encode

from zim.templates import get_template
from zim.export.linker import ExportLinker, StubLayout
from zim.export.template import ExportTemplateContext
from zim.export.exporters import createIndexPage

from zim.formats import get_format

logger = logging.getLogger('zim.www')


class WWWError(Error):
	'''Error with http error code'''

	#: mapping of error number to string - extend when needed
	statusstring = {
		'403': 'Forbidden',
		'404': 'Not Found',
		'405': 'Method Not Allowed',
		'500': 'Internal Server Error',
	}

	def __init__(self, msg, status='500', headers=None):
		'''Constructor
		@param msg: specific error message - will be appended after
		the standard error string
		@param status: error code, e.g. '500' for "Internal Server Error"
		or '404' for "Not Found" - see http specifications for valid
		error codes
		@param headers: additional http headers for the error response,
		list of 2-tuples with header name and value
		'''
		self.status = '%s %s' % (status, self.statusstring[status])
		self.headers = headers
		self.msg = self.status
		if msg:
			self.msg += ' - ' + msg


class WebPageNotFoundError(WWWError):
	'''Error whan a page is not found (404)'''

	description = '''\
You tried to open a page that does not exist.
'''

	def __init__(self, page):
		if not isinstance(page, str):
			page = page.name
		WWWError.__init__(self, 'No such page: %s' % page, status='404')


class WebPathNotValidError(WWWError):
	'''Error when the url points to an invalid page path'''

	description = '''\
The requested path is not valid
'''

	def __init__(self):
		WWWError.__init__(self, 'Invalid path', status='403')


class WWWInterface(object):
	'''Class to handle the WWW interface for zim notebooks.

	Objects of this class are callable, so they can be used as application
	objects within a WSGI compatible framework. See PEP 333 for details
	(U{http://www.python.org/dev/peps/pep-0333/}).

	For basic handlers to run this interface see the "wsgiref" package
	in the standard library for python.
	'''

	def __init__(self, notebook, template='Default', auth_creds=None):
		'''Constructor
		@param notebook: a L{Notebook} object
		@param template: html template for zim pages
		@param auth_creds: credentials for HTTP-authentication
		'''
		assert isinstance(notebook, Notebook)
		self.notebook = notebook
		self.auth_creds = auth_creds

		self.output = None

		if template is None:
			template = 'Default'

		self.template = get_template('html', template)
		self.linker_factory = partial(WWWLinker, self.notebook, self.template.resources_dir)
		self.dumper_factory = get_format('html').Dumper # XXX

		#~ self.notebook.indexer.check_and_update()

	def __call__(self, environ, start_response):
		'''Main function for handling a single request. Follows the
		WSGI API.

		@param environ: dictionary with environment variables for the
		request and some special variables. See the PEP for expected
		variables.

		@param start_response: a function that can be called to set the
		http response and headers. For example::

			start_response(200, [('Content-Type', 'text/plain')])

		@returns: the html page content as a list of lines
		'''
		if self.auth_creds:
			import base64

			def bad_auth():
				body = 'Please authenticate'
				realm = 'zimAuth'
				logger.info('Requesting Basic HTTP-Authentication')
				headers = [
					('Content-Type', 'text/plain'),
					('Content-Length', str(len(body))),
					('WWW-Authenticate', 'Basic realm="%s"' % realm)]
				start_response('401 Unauthorized', headers)
				return [body.encode()]

			auth = environ.get('HTTP_AUTHORIZATION')
			if auth:
				scheme, data = auth.split(None, 1)
				assert scheme.lower() == 'basic'
				username, password = base64.b64decode(data).decode('UTF-8').split(':')
				if username != self.auth_creds[0] or password != self.auth_creds[1]:
					return bad_auth()
				environ['REMOTE_USER'] = username
				del environ['HTTP_AUTHORIZATION']
			else:
				return bad_auth()

		headerlist = []
		headers = Headers(headerlist)
		path = environ.get('PATH_INFO', '/')
		path = path.encode('iso-8859-1').decode('UTF-8')
			# The WSGI standard mandates iso-8859-1, but we want UTF-8. See:
			# - https://www.python.org/dev/peps/pep-3333/#unicode-issues
			# - https://code.djangoproject.com/ticket/19468
		try:
			methods = ('GET', 'HEAD')
			if not environ['REQUEST_METHOD'] in methods:
				raise WWWError('405', headers=[('Allow', ', '.join(methods))])

			# cleanup path
			path = path.replace('\\', '/') # make it windows save
			isdir = path.endswith('/')
			parts = [p for p in path.split('/') if p and not p == '.']
			if [p for p in parts if p.startswith('.')]:
				# exclude .. and all hidden files from possible paths
				raise WebPathNotValidError()
			path = '/' + '/'.join(parts)
			if isdir and not path == '/':
				path += '/'

			if not path:
				path = '/'
			elif path == '/favicon.ico':
				path = '/+resources/favicon.ico'
			else:
				path = urllib.parse.unquote(path)

			if path == '/':
				headers.add_header('Content-Type', 'text/html', charset='utf-8')
				content = self.render_index()
			elif path.startswith('/+docs/'):
				dir = self.notebook.document_root
				if not dir:
					raise WebPageNotFoundError(path)
				file = dir.file(path[7:])
				file = adapt_from_oldfs(file)
				content = [file.read_binary()]
					# Will raise FileNotFound when file does not exist
				headers['Content-Type'] = file.mimetype()
			elif path.startswith('/+file/'):
				file = self.notebook.folder.file(path[7:])
					# TODO: need abstraction for getting file from top level dir ?
				file = adapt_from_oldfs(file)
				content = [file.read_binary()]
					# Will raise FileNotFound when file does not exist
				headers['Content-Type'] = file.mimetype()
			elif path.startswith('/+resources/'):
				if self.template.resources_dir:
					file = self.template.resources_dir.file(path[12:])
					if not file.exists():
						file = data_file('pixmaps/%s' % path[12:])
				else:
					file = data_file('pixmaps/%s' % path[12:])

				if file:
					file = adapt_from_oldfs(file)
					content = [file.read_binary()]
						# Will raise FileNotFound when file does not exist
					headers['Content-Type'] = file.mimetype()
				else:
					raise WebPageNotFoundError(path)
			else:
				# Must be a page or a namespace (html file or directory path)
				headers.add_header('Content-Type', 'text/html', charset='utf-8')
				if path.endswith('.html'):
					pagename = path[:-5].replace('/', ':')
				elif path.endswith('/'):
					pagename = path[:-1].replace('/', ':')
				else:
					raise WebPageNotFoundError(path)

				path = self.notebook.pages.lookup_from_user_input(pagename)
				try:
					page = self.notebook.get_page(path)
					if page.hascontent:
						content = self.render_page(page)
					elif page.haschildren:
						content = self.render_index(page)
					else:
						raise WebPageNotFoundError(path)
				except PageNotFoundError:
					raise WebPageNotFoundError(path)
		except Exception as error:
			headerlist = []
			headers = Headers(headerlist)
			headers.add_header('Content-Type', 'text/plain', charset='utf-8')
			if isinstance(error, (WWWError, FileNotFoundError)):
				logger.error(error.msg)
				if isinstance(error, FileNotFoundError):
					error = WebPageNotFoundError(path)
					# show url path instead of file path
				if error.headers:
					for key, value in error.headers:
						headers.add_header(key, value)
				start_response(error.status, headerlist)
				content = str(error).splitlines(True)
			# TODO also handle template errors as special here
			else:
				# Unexpected error - maybe a bug, do not expose output on bugs
				# to the outside world
				logger.exception('Unexpected error:')
				start_response('500 Internal Server Error', headerlist)
				content = ['Internal Server Error']

			if environ['REQUEST_METHOD'] == 'HEAD':
				return []
			else:
				return [c.encode('UTF-8') for c in content]
		else:
			start_response('200 OK', headerlist)
			if environ['REQUEST_METHOD'] == 'HEAD':
				return []
			elif content and isinstance(content[0], str):
				return [c.encode('UTF-8') for c in content]
			else:
				return content

	def render_index(self, namespace=None):
		'''Render an index page
		@param namespace: the namespace L{Path}
		@returns: html as a list of lines
		'''
		path = namespace or Path(':')
		page = createIndexPage(self.notebook, path, namespace)
		return self.render_page(page)

	def render_page(self, page):
		'''Render a single page from the notebook
		@param page: a L{Page} object
		@returns: html as a list of lines
		'''
		lines = []

		context = ExportTemplateContext(
			self.notebook,
			self.linker_factory,
			self.dumper_factory,
			title=page.get_title(),
			content=[page],
			home=self.notebook.get_home_page(),
			up=page.parent if page.parent and not page.parent.isroot else None,
			prevpage=self.notebook.pages.get_previous(page) if not page.isroot else None,
			nextpage=self.notebook.pages.get_next(page) if not page.isroot else None,
			links={'index': '/'},
			index_generator=self.notebook.pages.walk,
			index_page=page,
		)
		self.template.process(lines, context)
		return lines


class WWWLinker(ExportLinker):
	'''Implements a linker that returns the correct
	links for the way the server handles URLs.
	'''

	def __init__(self, notebook, resources_dir=None, source=None):
		layout = StubLayout(notebook, resources_dir)
		ExportLinker.__init__(self, notebook, layout, source=source)

	def icon(self, name):
		return url_encode('/+resources/%s.png' % name)

	def resource(self, path):
		return url_encode('/+resources/%s' % path)

	def resolve_source_file(self, link):
		return None # not used by HTML anyway

	def page_object(self, path):
		'''Turn a L{Path} object in a relative link or URI'''
		return url_encode('/' + encode_filename(path.name) + '.html')
			# TODO use script location as root for cgi-bin

	def file_object(self, file):
		'''Turn a L{File} object in a relative link or URI'''
		if file.ischild(self.notebook.folder):
			# attachment
			relpath = file.relpath(self.notebook.folder).replace(SEP, '/')
			return url_encode('/+file/' + relpath)
		elif self.notebook.document_root \
		and file.ischild(self.notebook.document_root):
			# document root
			relpath = file.relpath(self.notebook.document_root).replace(SEP, '/')
			return url_encode('/+docs/' + relpath)
			# TODO use script location as root for cgi-bin
			# TODO allow alternative document root for cgi-bin
		else:
			# external file -> file://
			return file.uri


def main(notebook, port=8080, public=True, **opts):
	httpd = make_server(notebook, port, public, **opts)
	logger.info("Serving HTTP on %s port %i...", httpd.server_name, httpd.server_port)
	httpd.serve_forever()


def make_server(notebook, port=8080, public=True, auth_creds=None, **opts):
	'''Create a simple http server
	@param notebook: the notebook location
	@param port: the http port to serve on
	@param public: allow connections to the server from other
	computers - if C{False} can only connect from localhost
	@param auth_creds: credentials for HTTP-authentication
	@param opts: options for L{WWWInterface.__init__()}
	@returns: a C{WSGIServer} object
	'''
	import wsgiref.simple_server
	app = WWWInterface(notebook, auth_creds=auth_creds, **opts) # FIXME make opts explicit
	if public:
		httpd = wsgiref.simple_server.make_server('', port, app)
	else:
		httpd = wsgiref.simple_server.make_server('localhost', port, app)
	return httpd
