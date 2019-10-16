class ObjectProxy:
    def __init__(self, target, invoker, target_callable=None):
        self._target = target
        self._invoker = invoker
        self._callable = target_callable

    # __getattr__ will be invoked for attributes haven't already been defined
    def __getattr__(self, name):
        # __getattribute__ will be invoked for all attributes
        target_attr = self._target.__getattribute__(name)
        if not callable(target_attr):
            return target_attr
        return ObjectProxy(self._target, self._invoker, target_callable=target_attr)

    def __call__(self, *args, **kwargs):
        return self._invoker(self._target, self._callable, *args, **kwargs)
