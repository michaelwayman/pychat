# PyChat

A terminal-based chat application written in python.

It is meant for demo/educational purposes and not for real-world adoption.

Check out the demo video at the bottom of this file and check out the [wiki](https://github.com/michaelwayman/pychat/wiki) for latest info

**Highlights:**
 - multiple users
 - customize how people see you
 - SSL encryption
 - scrollable chat history
 - 0 external dependencies
 - single file

**Requirements:**
 - `python >= 3.11`

___

## How to use


1. Someone runs the PyChat server
   1. `pychat.py --serve --host 0.0.0.0 --port 8080`
2. Others connect to the server
   1. `pychat.py --host 25.13.23.12 --port 8080`


### Things to know
1. Press the `tab` key to rotate focus between UI widgets
2. A widget must have focus to respond to keyboard & mouse events.
3. Press the `return` / `enter` key to send your message.


### SSL mode

The host may run the server in SSL mode.

Both the server and client must give valid certificates to connect via SSL.

I've included a script generates a self-signed CA (certificate of authority) and issue validation certificates, see next section.

Note: PyChat expects certfiles to combine the private key and certificate into a single file like so
```
-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
```

Note: For the central authority (CA) you shouldn't comvine the key and cert. Only the cert is used to confirm others and only the cert should be shared.
The CA key should be kept offline, air-gapped, in a dry-cool-place. The key is only needed to issue new certificates. 

#### Running SSL encryption looks like this:

```shell
# Server
> pychat.py -s --ssl --certfile ./server.pem --cafile ./rootCA.pem

# Client
> pychat.py --ssl --certfile ./client.pem --cafile ./rootCA.pem
```

#### Generate SSL certificates

The script I've included depends on having [openssl](https://github.com/openssl/openssl) installed on your system.

Once installed, you can generate the certificates by using the `Makefile`. (or examine the script and make it happen manually)

Another option for generating self-signed SSL certificates is to use a program like [mkcert](https://github.com/FiloSottile/mkcert), but it will essentially just do the same thing as the included script.


```shell
> make ssl_certs
```

This should create several files in the `./ssl_certs` folder
 - `rootCA.key` - The private key to the root certificate of authority (keep private, used to issue new certificates for others)
 - `rootCA.pem` - The certificate of authority. This is the file to use with pychat's `--cafile` option. (this file needs shared with others who want to connect via SSL) 
 - `client.pem` - A unique private key/certificate combo. The certificate is issued/signed by the CA. This is the file to use with the `--certfile` option.
 - `server.pem` - Same as client.pem except both are their own unique key/certificate combo.


### Full usage

```shell
> pychat.py --help
usage: pychat.py [-h] [-H HOST] [-P PORT] [-u USERNAME] [-c COLOR] [-s] [--ssl] [--certfile CERTFILE]
                 [--cafile CAFILE]

PyChat :)

PyChat is a terminal based multi-user, SSL enabled, chat application.

Within the app:
A widget must be 'in focus' to receive and respond to key-presses.
In other words, you must give focus to the input widget before typing your message.

The `tab` key rotates focus between UI widgets
The `return` key sends your typed message

options:
  -h, --help            show this help message and exit
  -H HOST, --host HOST  Host of sever
  -P PORT, --port PORT  Port of sever
  -u USERNAME, --username USERNAME
                        Display name to use in the chat
  -c COLOR, --color COLOR
                        Display color to use for your messages. (6 character hex string)
  -s, --serve           Run the chat server for others to connect
  --ssl                 Use secure connection via SSL
  --certfile CERTFILE   Path to SSL certificate
  --cafile CAFILE       Path to SSL certificate authority
```

___

## Demo reel

[pychat-demo.webm](https://github.com/michaelwayman/pychat/assets/5776784/943e18ae-e482-4115-835c-1e6ab7b9695e)

___

## High level architecture

![high-level-md](https://github.com/michaelwayman/pychat/assets/5776784/23736bea-3a2b-4fe7-b300-d6baf4c57aa3)
