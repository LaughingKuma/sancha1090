def put(store, key, value, now, max_size) -> None:
    # Every serving cache stores (expiry, ...), so eviction reads value[0] and stays blind to the rest of the shape.
    store[key] = value
    if len(store) <= max_size:
        return
    # entries only age out on same-key hits, so a many-key sweep would grow the dict unboundedly
    for k in [k for k, v in store.items() if v[0] <= now]:
        del store[k]
    while len(store) > max_size:
        del store[min(store, key=lambda k: store[k][0])]
