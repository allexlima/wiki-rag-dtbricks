FROM mediawiki:1.42

# The base mediawiki image only includes MySQL/MariaDB PHP drivers.
# Lakebase is PostgreSQL — install the pgsql extension.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev \
    && docker-php-ext-install pgsql \
    && rm -rf /var/lib/apt/lists/*

# Create cache directory for file-based L10n cache
RUN mkdir -p /var/www/html/cache \
    && chown www-data:www-data /var/www/html/cache
