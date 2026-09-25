from feedback import from_environment, make_wsgi
application = make_wsgi(from_environment())
