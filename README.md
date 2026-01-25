This Repo accomplishes the following:
1) Create DDLs in the format expected by DATAGENX: https://github.com/kennytm/dbgen
2) Use the .dbgen files created above to generate the INSERT statements.


mysql_sakila.py does the following:
1) Connect to a mysql database. 
2) Get the schema for the "sakila" database.
3) Create a new directory with a timestamp suffix.
4) Create one file per table with the DDL in it.
5) The DDL is created by reading the latest histogram information.

At the end of this process a new directory will be created looking like this:

ls dbgen_output_20251229_155143
actor.dbgen		country.dbgen		film_text.dbgen		payment.dbgen
address.dbgen		customer.dbgen		film.dbgen		rental.dbgen
category.dbgen		film_actor.dbgen	inventory.dbgen		staff.dbgen
city.dbgen		film_category.dbgen	language.dbgen		store.dbgen


run_sakila.py does the following:
1) Use the .dbgen files created above, call dbgen script for each of them.
2) Keep these scripts in a local output directory.

python3 run_sakila.py
ls dbgen_tmp_out
actor-schema.sql		customer-schema.sql		language-schema.sql
actor.1.sql			customer.1.sql			language.1.sql
address-schema.sql		film_actor-schema.sql		payment-schema.sql
address.1.sql			film_actor.1.sql		payment.1.sql
category-schema.sql		film_category-schema.sql	rental-schema.sql
category.1.sql			film_category.1.sql		rental.1.sql
city-schema.sql			film_text-schema.sql		staff-schema.sql
city.1.sql			film_text.1.sql			staff.1.sql
country-schema.sql		inventory-schema.sql		store-schema.sql
country.1.sql			inventory.1.sql			store.1.sql


3)replay_and_validate_sakila.py

This script does the following:
1) Create the table present in orders-schema.sql
2) Populate the new table with orders.1.sql.
3) Compare stats for original table and new table.
4) Drop the newly created table 

python replay_and_validate_sakila.py \
  --user root \
  --password newpassword \
  --source-schema=tpch \
  --ddl-file orders-schema.sql \
  --insert-file orders.1.sql  


<img width="1750" height="903" alt="CreateTablesAndCompare" src="https://github.com/user-attachments/assets/c314602f-18b4-4553-989d-dbe18284d01d" /><img width="1439" height="533" alt="CreateDbgenFiles" src="https://github.com/user-attachments/assets/457442dd-ef84-4fc7-8efe-b1b10a0cfdc7" />

<img width="1595" height="385" alt="DbGenFiles" src="https://github.com/user-attachments/assets/725726c8-67a3-4f34-ab8b-5e4ea41d388a" />


<img width="1920" height="883" alt="CreateInsertFiles" src="https://github.com/user-attachments/assets/8a926494-b5b9-4521-b1ca-fa307c865a4b" />


<img width="1357" height="632" alt="InsetFiles" src="https://github.com/user-attachments/assets/fd53210d-3bc2-4a2c-b025-aeafea60d154" />



<img width="1750" height="903" alt="CreateTablesAndCompare" src="https://github.com/user-attachments/assets/81177cdd-a67c-4e55-9f21-0c6d23bb9b2d" />





Check the contents of each of these files. The 1.sql files should contain INSERT statements. 
