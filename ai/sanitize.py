#!/usr/bin/env python3
import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session
from models.base import Product, ProductVariant, Item

load_dotenv()

client = OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
)

engine = create_engine(os.environ.get("DATABASE_PROD_URL"))


def main():
    # create database session
    with Session(engine) as session:

        # query products (chunk in prod)
        products = session.query(Product).all()

        for product in products:
            print(f"Processing: {product.id} - {product.name}")
            # Parse gear into product name & variants
            completion = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a backpacking gear directory containing official product names."},
                    {"role": "user",
                        "content": f"Parse \"{product.name}\""}
                ],
                functions=[
                    {
                        "name": "parseGear",
                        "description": "Parse backpacking gear string into product name and variants.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "product_name": {
                                    "type": "string",
                                },
                                "variants": {
                                    "type": "string",
                                    "description": "Variants of the product (size, gender, color, weight, dimensions, volume, accessories)"
                                }
                            },
                            "required": ["parseGear"]
                        }
                    }
                ],
                function_call={"name": "parseGear"}
            )

            func = completion.choices[0].message.function_call

            try:
                res = json.loads(func.arguments)
            except json.decoder.JSONDecodeError as e:
                print(f"Error parsing JSON: {e}")
                continue

            product_name = res.get("product_name")
            variants = res.get("variants")

            print(f"Product Name: {product_name}")
            print(f"Variants: {variants}")

            if not product_name:
                print("Skipping...\n")
                continue

            variant_product_id = product.id
            reassociate_product = False
            try:
                product.name = product_name
                session.commit()
            except Exception:
                session.rollback()
                reassociate_product = True

            if reassociate_product:
                print("Reassociating product...")
                # check if product exists
                existing_product = session.query(Product).filter(
                    Product.name == product_name).first()

                if existing_product:
                    # associate existing product with existing data
                    try:
                        session.execute(update(Item).where(Item.product_id == product.id,
                                        Item.brand_id == product.brand_id).values(product_id=existing_product.id))
                        session.commit()
                        variant_product_id = existing_product.id

                        # delete unused duplicate product
                        session.delete(product)
                        session.refresh(existing_product)
                        session.commit()
                    except Exception:
                        session.rollback()
                        print("Error associating existing product with existing data")
                        continue

            reassociate_variant = False
            if variants:
                try:
                    new_variant = ProductVariant(
                        product_id=variant_product_id, name=variants)
                    session.add(new_variant)
                    session.commit()
                except Exception:
                    session.rollback()
                    reassociate_variant = True

                if reassociate_variant:
                    print("Reassociating variant...")
                    # check if variant exists
                    existing_variant = session.query(ProductVariant).filter(
                        ProductVariant.product_id == variant_product_id, ProductVariant.name == variants).first()

                    if existing_variant:
                        # associate existing variant with existing data
                        if existing_product:
                            existing_variant.product_id = existing_product.id
                        else:
                            existing_variant.product_id = product.id

                        try:
                            session.commit()
                        except Exception:
                            session.rollback()
                            print(
                                "Error associating existing variant with existing data")

            print("\n")


if __name__ == "__main__":
    main()
