import functools
import inspect
import eventlet

from urchin import safe_utils


def expects_func_args(*args):
    def _decorator_checker(dec):
        @functools.wraps(dec)
        def _decorator(f):
            base_f = safe_utils.get_wrapped_function(f)
            arg_names, a, kw, _default = inspect.getargspec(base_f)
            if a or kw or set(args) <= set(arg_names):
                # NOTE (ndipanov): We can't really tell if correct stuff will
                # be passed if it's a function with *args or **kwargs so
                # we still carry on and hope for the best
                return dec(f)
            else:
                raise TypeError("Decorated function %(f_name)s does not "
                                "have the arguments expected by the "
                                "decorator %(d_name)s" %
                                {'f_name': base_f.__name__,
                                 'd_name': dec.__name__})
        return _decorator
    return _decorator_checker


def spawn(func, *args, **kwargs):
    """Passthrough method for eventlet.spawn.

    This utility exists so that it can be stubbed for testing without
    interfering with the service spawns.

    It will also grab the context from the threadlocal store and add it to
    the store on the new thread.  This allows for continuity in logging the
    context when using this method to spawn a new thread.
    """

    @functools.wraps(func)
    def context_wrapper(*args, **kwargs):
        # NOTE: If update_store is not called after spawn it won't be
        # available for the logger to pull from threadlocal storage.
        if _context is not None:
            _context.update_store()
        return func(*args, **kwargs)

    return eventlet.spawn(context_wrapper, *args, **kwargs)