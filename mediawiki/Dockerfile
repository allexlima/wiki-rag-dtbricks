FROM mediawiki:1.42

# The base mediawiki image only includes MySQL/MariaDB PHP drivers.
# Lakebase is PostgreSQL — install the pgsql extension.
# gettext-base provides envsubst for the ECS entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gettext-base \
    && docker-php-ext-install pgsql \
    && rm -rf /var/lib/apt/lists/*

# Create cache directory for file-based L10n cache
RUN mkdir -p /var/www/html/cache \
    && chown www-data:www-data /var/www/html/cache

# Copy config template + entrypoint for ECS self-configuration
COPY LocalSettings.php.template /var/www/html/LocalSettings.php.template
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["apache2-foreground"]
