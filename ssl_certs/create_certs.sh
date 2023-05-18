#! /bin/bash

issue_certificate () {
  local filename=$1
  # Create private key
  openssl genrsa -out "$filename.key" 2048
  # Create signing request
  openssl req -new -key "$filename.key" -out "$filename.csr" -config csr.cnf
  # Determine which option to use for the serial number
  if [ -f "rootCA.srl" ]; then
    serial_option="-CAserial rootCA.srl"
  else
    serial_option=-CAcreateserial
  fi
  # Create server certificate
  openssl x509 -req \
               -in "$filename.csr" \
               -CA rootCA.pem -CAkey rootCA.key \
               -out "$filename.crt" \
               -sha256 -days 36500 \
               $serial_option \
               -extfile cert.cnf
  # Cleanup
  cat "$filename.key" "$filename.crt" > "$filename.pem"
  rm "$filename.key" "$filename.crt" "$filename.csr"
}


create_ca () {
  openssl req -x509 \
              -sha256 -days 36500 \
              -nodes \
              -newkey rsa:2048 \
              -subj "/CN=localhost/C=NA/L=Redacted" \
              -keyout ./rootCA.key -out ./rootCA.crt
  # Cleanup
  cat rootCA.crt > rootCA.pem
  rm rootCA.crt
}

if [ "$1" = "ca" ]; then
  # Create CA
  create_ca
elif [ "$1" = "issue" ]; then
  # Create a client & server certificate
  issue_certificate "$2"
else
  # Print usage
  echo "Usage: $0 [command]"
  echo "ca               create a certificate of authority"
  echo "issue NAME       use the CA to issue a certificate with the given NAME"
fi
