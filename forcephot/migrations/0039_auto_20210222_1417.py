# Generated by Django 3.1.7 on 2021-02-22 14:17

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('forcephot', '0038_auto_20210222_1250'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='from_api',
            field=models.BooleanField(default=False),
        ),
    ]
