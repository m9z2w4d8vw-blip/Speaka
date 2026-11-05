/* BTPSDaemonProbe.c - the loopback connect the audio relay uses to wake the helper (BTPSV88Daemon.c).
 * The v7.4/v7.5 probes that once lived here, which sent test PCM and a recorded SBC fixture to a helper started by
 * hand over SSH, were removed with the helper's test modes. */
#include "BTPSDaemonProbe.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdint.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static int timed_connect_port(int fd, uint16_t port)
{
    int flags=fcntl(fd,F_GETFL,0);
    if(flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)<0) return -1;
    struct sockaddr_in address={0};
    address.sin_family=AF_INET;
    address.sin_port=htons(port);
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    int r=connect(fd,(const struct sockaddr *)&address,sizeof address);
    if(r<0 && errno!=EINPROGRESS) return -1;
    if(r<0) {
        fd_set wfds;
        FD_ZERO(&wfds);FD_SET(fd,&wfds);
        struct timeval timeout={1,0};
        if(select(fd+1,NULL,&wfds,NULL,&timeout)!=1) { errno=ETIMEDOUT;return -1; }
        int e=0;socklen_t n=sizeof e;
        if(getsockopt(fd,SOL_SOCKET,SO_ERROR,&e,&n)<0) return -1;
        if(e) {errno=e;return -1;}
    }
    if(fcntl(fd,F_SETFL,flags)<0) return -1;
    struct timeval timeout={2,0};
    (void)setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof timeout);
    (void)setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
    int nosigpipe=1;(void)setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&nosigpipe,sizeof nosigpipe);
    return 0;
}

int BTPSDaemonConnectLoopback(uint16_t port)
{
    int fd=socket(AF_INET,SOCK_STREAM,0);
    if(fd>=0 && timed_connect_port(fd,port)<0) {close(fd);return -1;}
    return fd;
}
