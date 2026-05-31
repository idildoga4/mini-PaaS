-- postgres-init/01_create_databases.sql
-- PostgreSQL container ilk başladığında bu script otomatik çalışır.
-- auth_db zaten POSTGRES_DB ile oluşturuluyor, deploy_db ve github_db burada ekleniyor.
-- NOT: Bu script sadece container ilk kez başladığında çalışır (postgres-data volume boşsa).

CREATE DATABASE deploy_db OWNER paas_user;
CREATE DATABASE github_db OWNER paas_user;
