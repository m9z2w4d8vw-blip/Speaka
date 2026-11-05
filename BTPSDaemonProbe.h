#pragma once
#include <stdint.h>

/* Connects to 127.0.0.1:port within a second, with 2 s send and receive timeouts. -1 if that fails. */
int BTPSDaemonConnectLoopback(uint16_t port);
