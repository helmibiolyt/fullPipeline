/** @type {import('next').NextConfig} */
export default {
  // The AWS SDK is imported only from route handlers, never from a client
  // component - the credentials must not end up in a browser bundle. On
  // Next 14 the key is experimental.serverComponentsExternalPackages; the
  // Next 15 spelling (serverExternalPackages) is silently ignored here, which
  // is worth knowing because the warning is easy to scroll past.
  experimental: { serverComponentsExternalPackages: ['@aws-sdk/client-s3'] },
}
