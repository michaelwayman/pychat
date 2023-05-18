# PyChat

A terminal-based chat application written in python.

It is meant for demo/educational purposes and not for real-world adoption. 

**Highlights:**
 - multiple users
 - SSL encryption
 - scrollable chat history
 - 0 external dependencies
 - single file

**Requirements:**
 - `python >= 3.11`

___

## How to use


### Example steps
1. Someone runs the PyChat server
   1. `pychat.py --serve --host 0.0.0.0 --port 8080`
2. Others connect to the server
   1. `pychat.py --host 25.13.23.12 --port 8080`


### Things to know
1. There are 2 "windows" in the PyChat app.
   1. The chat window (shows all the messages)
   2. The input window (where you type your messages)
2. The `tab` key rotates focus between windows.
3. A border surrounds whichever window has focus.
4. A window must be in focus to respond to typing & scrolling.
5. To type your message give the "input window" focus by pressing the `tab` key.
6. Press the `return` / `enter` key to send your message.


### SSL mode

The host may run the server in SSL mode.

When SSL mode is on, both the server and client must provide certificates the other trusts.

PyChat will load the system's default SSL verification certs, but most likely you will need to provide your own.

In the spirit of that, I've included a script that will generate a self-signed CA (certificate of authority) and issue validation certificates. More information below.

Note: it is expected that the certfiles should combine the private key and certificate into a single file like so
```
-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
```

Note: DO NOT combine the CA certificate with its key. Only the CA certificate is needed to validate other certificates.
The CA key is only needed to issue new certificates. It should be kept offline, air-gapped, in a dry-cool-place.

With this in mind, running SSL encryption would look like this:

```shell
# Server
> pychat.py -s --ssl --certfile ./server.pem --cafile ./rootCA.pem

# Client
> pychat.py --ssl --certfile ./client.pem --cafile ./rootCA.pem
```

### Generate SSL certificates

The script I've included depends on having [openssl](https://github.com/openssl/openssl) installed on your system.

Once installed, you have a couple options:

#### Option 1 - Use the Makefile

```shell
> make ssl_certs
```

This should create several files in the `./ssl_certs` folder
 - `rootCA.key` - The private key to the root certificate of authority
 - `rootCA.pem` - The certificate of authority. This is the file to use with pychat's `--cafile` option. 
 - `client.pem` - A unique private key/certificate combo. The certificate is issued by the CA. This is the file to use with the `--certfile` option.
 - `server.pem` - Same as client.pem except both are their own unique key/certificate combo.

#### Option 2 - Run the script yourself

Run the script manually

#### Option 3
Use another 3rd party program like [mkcert](https://github.com/FiloSottile/mkcert)


### Full usage

```shell
 > pychat.py --help
usage: app.py [-h] [-H HOST] [-P PORT] [-s] [--debug] [--ssl] [--certfile CERTFILE] [--cafile CAFILE]

PyChat :)

options:
  -h, --help            show this help message and exit
  -H HOST, --host HOST  Host of sever
  -P PORT, --port PORT  Port of sever
  -s, --serve           Run the chat server for others to connect
  --debug               Turn on debug mode
  --ssl                 Use secure connection via SSL
  --certfile CERTFILE   Path to SSL certificate
  --cafile CAFILE       Path to SSL certificate authority
```
